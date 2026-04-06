"""
Machine learning signal model using XGBoost.
Predicts forward returns classification (up / down / flat).
"""

import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from xgboost import XGBClassifier
from sklearn.metrics import accuracy_score, classification_report

from trading_system.config.settings import MLConfig

logger = logging.getLogger(__name__)


class MLSignalModel:
    """XGBoost-based signal model with walk-forward training."""

    LABELS = {0: "DOWN", 1: "FLAT", 2: "UP"}

    def __init__(self, config: MLConfig | None = None):
        self.config = config or MLConfig()
        self.model: XGBClassifier | None = None
        self.feature_names: list[str] = []
        self.feature_importance: dict[str, float] = {}
        self.is_trained = False

    def prepare_target(self, df: pd.DataFrame, price_col: str = "Adj Close") -> pd.Series:
        """
        Create classification target based on forward returns.
        0 = DOWN (return < -threshold)
        1 = FLAT (|return| <= threshold)
        2 = UP   (return > threshold)
        """
        fwd_return = df[price_col].pct_change(self.config.target_horizon).shift(-self.config.target_horizon)
        threshold = self.config.classification_threshold

        target = pd.Series(1, index=df.index, name="target")  # default FLAT
        target[fwd_return > threshold] = 2   # UP
        target[fwd_return < -threshold] = 0  # DOWN
        target[fwd_return.isna()] = np.nan

        return target

    def train(self, X: pd.DataFrame, y: pd.Series) -> dict:
        """Train the XGBoost classifier."""
        # Drop NaN targets — use .values to avoid index alignment issues
        mask = y.notna().values & X.notna().all(axis=1).values
        X_clean = X.loc[mask].reset_index(drop=True)
        y_clean = y.loc[mask].reset_index(drop=True).astype(int)

        if len(X_clean) < 100:
            logger.warning(f"Only {len(X_clean)} samples, need at least 100 for training")
            return {"status": "insufficient_data", "n_samples": len(X_clean)}

        self.feature_names = list(X_clean.columns)

        self.model = XGBClassifier(
            n_estimators=self.config.n_estimators,
            max_depth=self.config.max_depth,
            learning_rate=self.config.learning_rate,
            min_child_weight=self.config.min_child_weight,
            subsample=self.config.subsample,
            colsample_bytree=self.config.colsample_bytree,
            objective="multi:softprob",
            num_class=3,
            use_label_encoder=False,
            eval_metric="mlogloss",
            random_state=42,
            n_jobs=-1,
            verbosity=0,
        )

        self.model.fit(X_clean, y_clean)

        # Feature importance
        importances = self.model.feature_importances_
        self.feature_importance = dict(
            sorted(zip(self.feature_names, importances), key=lambda x: x[1], reverse=True)
        )

        # Training accuracy
        train_pred = self.model.predict(X_clean)
        train_acc = accuracy_score(y_clean, train_pred)

        self.is_trained = True
        logger.info(f"Model trained on {len(X_clean)} samples, train accuracy: {train_acc:.3f}")

        return {
            "status": "trained",
            "n_samples": len(X_clean),
            "train_accuracy": train_acc,
            "class_distribution": y_clean.value_counts().to_dict(),
            "top_features": dict(list(self.feature_importance.items())[:10]),
        }

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """
        Generate predictions with probabilities.
        Returns DataFrame with columns: prediction, prob_down, prob_flat, prob_up, confidence
        """
        if not self.is_trained or self.model is None:
            raise RuntimeError("Model not trained. Call train() first.")

        # Align features
        X_aligned = X[self.feature_names].copy()

        # Handle NaN — fill with 0 for prediction (flagged separately)
        has_nan = X_aligned.isna().any(axis=1)
        X_filled = X_aligned.fillna(0)

        # Predict probabilities
        probs = self.model.predict_proba(X_filled)
        preds = self.model.predict(X_filled)

        result = pd.DataFrame(index=X.index)
        result["prediction"] = preds
        result["prediction_label"] = result["prediction"].map(self.LABELS)
        result["prob_down"] = probs[:, 0]
        result["prob_flat"] = probs[:, 1]
        result["prob_up"] = probs[:, 2]

        # Confidence = max probability - second highest probability
        sorted_probs = np.sort(probs, axis=1)
        result["confidence"] = sorted_probs[:, -1] - sorted_probs[:, -2]

        # Flag rows with missing data
        result["data_quality"] = (~has_nan).astype(float)

        return result

    def evaluate(self, X: pd.DataFrame, y: pd.Series) -> dict:
        """Evaluate model on test data."""
        mask = y.notna().values & X.notna().all(axis=1).values
        X_clean = X.loc[mask].reset_index(drop=True)
        y_clean = y.loc[mask].reset_index(drop=True).astype(int)

        if len(X_clean) == 0:
            return {"status": "no_valid_data"}

        preds = self.model.predict(X_clean)
        acc = accuracy_score(y_clean, preds)

        report = classification_report(y_clean, preds, target_names=["DOWN", "FLAT", "UP"], output_dict=True)

        return {
            "accuracy": acc,
            "n_samples": len(X_clean),
            "report": report,
            "class_distribution_actual": y_clean.value_counts().to_dict(),
            "class_distribution_predicted": pd.Series(preds).value_counts().to_dict(),
        }

    def walk_forward_train(
        self,
        features_dict: dict[str, pd.DataFrame],
        feature_cols: list[str],
        train_days: int = 504,
        test_days: int = 63,
        step_days: int = 21,
        purge_days: int = 5,
    ) -> list[dict]:
        """
        Walk-forward training and evaluation.
        features_dict: ticker -> DataFrame with features and target column
        Returns list of evaluation results per fold.
        """
        # Combine all tickers into one dataset
        all_data = []
        for ticker, df in features_dict.items():
            if "target" not in df.columns:
                continue
            chunk = df[feature_cols + ["target"]].copy()
            chunk["ticker"] = ticker
            all_data.append(chunk)

        if not all_data:
            return [{"status": "no_data"}]

        combined = pd.concat(all_data).reset_index()
        date_col = combined.columns[0]  # the original index (Date)
        dates = np.sort(combined[date_col].unique())

        results = []
        fold = 0

        i = 0
        while i + train_days + purge_days + test_days <= len(dates):
            train_end_idx = i + train_days
            test_start_idx = train_end_idx + purge_days
            test_end_idx = test_start_idx + test_days

            train_dates = set(dates[i:train_end_idx])
            test_dates_arr = dates[test_start_idx:test_end_idx]
            test_dates = set(test_dates_arr)

            train_mask = combined[date_col].isin(train_dates)
            test_mask = combined[date_col].isin(test_dates)

            train_subset = combined.loc[train_mask].reset_index(drop=True)
            test_subset = combined.loc[test_mask].reset_index(drop=True)

            X_train = train_subset[feature_cols]
            y_train = train_subset["target"]
            X_test = test_subset[feature_cols]
            y_test = test_subset["target"]

            train_result = self.train(X_train, y_train)
            if train_result["status"] != "trained":
                i += step_days
                continue

            eval_result = self.evaluate(X_test, y_test)
            eval_result["fold"] = fold
            train_dates_sorted = sorted(train_dates)
            eval_result["train_start"] = str(pd.Timestamp(train_dates_sorted[0]).date())
            eval_result["train_end"] = str(pd.Timestamp(train_dates_sorted[-1]).date())
            eval_result["test_start"] = str(pd.Timestamp(test_dates_arr[0]).date())
            eval_result["test_end"] = str(pd.Timestamp(test_dates_arr[-1]).date())
            results.append(eval_result)

            fold += 1
            i += step_days

        if results:
            avg_acc = np.mean([r["accuracy"] for r in results if "accuracy" in r])
            logger.info(f"Walk-forward: {len(results)} folds, avg accuracy: {avg_acc:.3f}")

        return results

    def save(self, path: str | Path):
        """Save model to disk."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "feature_names": self.feature_names,
                "feature_importance": self.feature_importance,
                "config": self.config,
            }, f)

    def load(self, path: str | Path):
        """Load model from disk."""
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.model = data["model"]
        self.feature_names = data["feature_names"]
        self.feature_importance = data["feature_importance"]
        self.is_trained = True
