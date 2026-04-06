"""
Signal combiner: merges rule-based and ML signals into final trading decisions.
"""

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd


@dataclass
class TradingSignal:
    """A single trading signal for a ticker."""
    ticker: str
    direction: str  # BUY, SELL, HOLD
    confidence: float  # 0.0 to 1.0
    entry_price: float
    stop_loss: float
    target_price: float
    risk_reward_ratio: float
    reasoning: list[str] = field(default_factory=list)
    model_contributions: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self):
        rr = f"{self.risk_reward_ratio:.1f}" if self.risk_reward_ratio else "N/A"
        reasons = " | ".join(self.reasoning[:3])
        return (
            f"{self.direction:4s} {self.ticker:6s} "
            f"conf={self.confidence:.2f} "
            f"entry=${self.entry_price:.2f} "
            f"stop=${self.stop_loss:.2f} "
            f"target=${self.target_price:.2f} "
            f"R:R={rr} "
            f"[{reasons}]"
        )


class SignalCombiner:
    """Combines rule-based, ML, and sentiment signals into final trading signals."""

    def __init__(self, rule_weight: float = 0.5, ml_weight: float = 0.5):
        self.rule_weight = rule_weight
        self.ml_weight = ml_weight
        self.sentiment_data: dict[str, dict] = {}  # ticker -> sentiment result

    def set_sentiment_data(self, sentiment_data: dict[str, dict]):
        """Inject sentiment data from scrapers."""
        self.sentiment_data = sentiment_data

    def combine(
        self,
        ticker: str,
        features_df: pd.DataFrame,
        rule_signals: pd.DataFrame,
        ml_predictions: pd.DataFrame | None = None,
    ) -> TradingSignal | None:
        """
        Combine signals for the latest data point and produce a TradingSignal.
        Returns None if no actionable signal.
        """
        if features_df.empty:
            return None

        latest_idx = features_df.index[-1]
        price = features_df.loc[latest_idx, "Adj Close"] if "Adj Close" in features_df.columns else features_df.loc[latest_idx, "Close"]
        atr = features_df.loc[latest_idx, "atr"] if "atr" in features_df.columns else price * 0.02

        # Rule-based score
        rule_score = rule_signals.loc[latest_idx, "rule_score"] if latest_idx in rule_signals.index else 0.0
        rule_conf = rule_signals.loc[latest_idx, "rule_confidence"] if latest_idx in rule_signals.index else 0.0
        rule_reason = rule_signals.loc[latest_idx, "rule_reasons"] if latest_idx in rule_signals.index else ""

        # ML score
        ml_score = 0.0
        ml_conf = 0.0
        ml_reason = ""
        contributions = {"rule_based": rule_score}

        if ml_predictions is not None and latest_idx in ml_predictions.index:
            ml_row = ml_predictions.loc[latest_idx]
            # Convert prediction to score: UP=+1, FLAT=0, DOWN=-1
            ml_pred_score = {0: -1.0, 1: 0.0, 2: 1.0}.get(int(ml_row["prediction"]), 0.0)
            ml_score = ml_pred_score * ml_row["confidence"]
            ml_conf = ml_row["confidence"]
            ml_reason = f"ML predicts {ml_row['prediction_label']} ({ml_row['confidence']:.0%} conf)"
            contributions["ml_model"] = ml_score

            # Use both weights
            combined_score = (rule_score * self.rule_weight + ml_score * self.ml_weight)
            combined_conf = (rule_conf * self.rule_weight + ml_conf * self.ml_weight)
        else:
            # Rule-based only
            combined_score = rule_score
            combined_conf = rule_conf

        # Sentiment adjustment — boost or penalize based on social/news sentiment
        sentiment_reason = ""
        sent = self.sentiment_data.get(ticker)
        if sent and sent.get("combined_score", 0) != 0:
            sent_score = sent["combined_score"]
            sent_label = sent.get("label", "neutral")
            contributions["sentiment"] = sent_score

            # Sentiment confirms signal direction: boost confidence
            # Sentiment contradicts signal direction: reduce confidence
            if (combined_score > 0 and sent_score > 0.1) or (combined_score < 0 and sent_score < -0.1):
                combined_conf *= 1.15  # 15% confidence boost for agreement
                sentiment_reason = f"Sentiment confirms ({sent_label}, score={sent_score:+.2f})"
            elif (combined_score > 0 and sent_score < -0.15) or (combined_score < 0 and sent_score > 0.15):
                combined_conf *= 0.70  # 30% confidence penalty for contradiction
                sentiment_reason = f"Sentiment CONTRADICTS ({sent_label}, score={sent_score:+.2f})"
            else:
                sentiment_reason = f"Sentiment neutral ({sent_score:+.2f})"

        # Determine direction
        if combined_score > 0.15:
            direction = "BUY"
        elif combined_score < -0.15:
            direction = "SELL"
        else:
            direction = "HOLD"

        # Skip low-confidence signals
        if combined_conf < 0.15 or direction == "HOLD":
            return None

        # Calculate stop loss and target based on ATR
        if direction == "BUY":
            stop_loss = price - 2.0 * atr
            target = price + 3.0 * atr
        else:
            stop_loss = price + 2.0 * atr
            target = price - 3.0 * atr

        risk = abs(price - stop_loss)
        reward = abs(target - price)
        rr_ratio = reward / risk if risk > 0 else 0.0

        # Build reasoning list
        reasoning = []
        if rule_reason and rule_reason != "No strong signals":
            reasoning.extend(rule_reason.split("; "))
        if ml_reason:
            reasoning.append(ml_reason)
        if sentiment_reason:
            reasoning.append(sentiment_reason)

        return TradingSignal(
            ticker=ticker,
            direction=direction,
            confidence=min(combined_conf, 1.0),
            entry_price=round(price, 2),
            stop_loss=round(stop_loss, 2),
            target_price=round(target, 2),
            risk_reward_ratio=round(rr_ratio, 2),
            reasoning=reasoning,
            model_contributions=contributions,
        )

    def generate_signals_batch(
        self,
        features_dict: dict[str, pd.DataFrame],
        rule_signals_dict: dict[str, pd.DataFrame],
        ml_predictions_dict: dict[str, pd.DataFrame] | None = None,
    ) -> list[TradingSignal]:
        """Generate signals for all tickers, sorted by confidence."""
        signals = []

        for ticker, features in features_dict.items():
            rule_sigs = rule_signals_dict.get(ticker)
            ml_preds = ml_predictions_dict.get(ticker) if ml_predictions_dict else None

            if rule_sigs is None:
                continue

            signal = self.combine(ticker, features, rule_sigs, ml_preds)
            if signal is not None:
                signals.append(signal)

        # Sort by confidence descending
        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals
