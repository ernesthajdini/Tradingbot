"""
Feature engineering engine.
Computes technical indicators and derived features from OHLCV data.
"""

import numpy as np
import pandas as pd

from trading_system.config.settings import FeatureConfig


class FeatureEngine:
    """Computes technical indicators and features for model input."""

    def __init__(self, config: FeatureConfig | None = None):
        self.config = config or FeatureConfig()

    def compute_all(
        self,
        df: pd.DataFrame,
        benchmark_df: pd.DataFrame | None = None,
        vix_df: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """
        Compute all features from OHLCV data.
        Input df must have columns: Open, High, Low, Close, Volume, Adj Close
        Optional: benchmark_df (SPY) for cross-sectional features
                  vix_df (^VIX) for macro features
        Returns DataFrame with original columns + all computed features.
        """
        feat = df.copy()
        price = feat["Adj Close"] if "Adj Close" in feat.columns else feat["Close"]

        # --- Trend indicators ---
        feat = self._add_moving_averages(feat, price)
        feat = self._add_macd(feat, price)

        # --- Momentum indicators ---
        feat = self._add_rsi(feat, price)
        feat = self._add_stochastic(feat)
        feat = self._add_rate_of_change(feat, price)

        # --- Volatility indicators ---
        feat = self._add_bollinger_bands(feat, price)
        feat = self._add_atr(feat)
        feat["historical_volatility_20"] = price.pct_change().rolling(20).std() * np.sqrt(252)

        # --- Volume indicators ---
        feat = self._add_volume_features(feat, price)

        # --- Price structure ---
        feat = self._add_price_structure(feat, price)

        # --- Returns at various horizons ---
        for n in [1, 5, 10, 20]:
            feat[f"return_{n}d"] = price.pct_change(n)

        # --- Cross-sectional: relative strength vs benchmark (SPY) ---
        if benchmark_df is not None and not benchmark_df.empty:
            feat = self._add_relative_strength(feat, price, benchmark_df)

        # --- Macro: VIX context ---
        if vix_df is not None and not vix_df.empty:
            feat = self._add_macro_context(feat, vix_df)

        # --- Drop rows with NaN from warmup period ---
        # Don't drop here; let the caller decide based on the longest indicator period

        return feat

    def _add_relative_strength(self, df: pd.DataFrame, price: pd.Series, bench: pd.DataFrame) -> pd.DataFrame:
        """Cross-sectional features: how does this ticker rank vs the market."""
        bench_price = bench["Adj Close"] if "Adj Close" in bench.columns else bench["Close"]
        bench_aligned = bench_price.reindex(df.index, method="ffill")

        # Relative return (ticker - SPY) at multiple horizons
        for n in [5, 20, 60]:
            ticker_ret = price.pct_change(n)
            bench_ret = bench_aligned.pct_change(n)
            df[f"rel_strength_{n}d"] = ticker_ret - bench_ret

        # Beta proxy: rolling correlation
        ticker_daily = price.pct_change()
        bench_daily = bench_aligned.pct_change()
        df["beta_60d"] = ticker_daily.rolling(60).cov(bench_daily) / bench_daily.rolling(60).var().replace(0, np.nan)

        # Outperformance flag
        df["outperforming_spy_20d"] = (df["rel_strength_20d"] > 0).astype(int)
        return df

    def _add_macro_context(self, df: pd.DataFrame, vix: pd.DataFrame) -> pd.DataFrame:
        """Macro features: VIX level, regime context."""
        vix_close = vix["Close"] if "Close" in vix.columns else vix["Adj Close"]
        vix_aligned = vix_close.reindex(df.index, method="ffill")

        df["vix_level"] = vix_aligned
        df["vix_ma_20"] = vix_aligned.rolling(20).mean()
        df["vix_relative"] = vix_aligned / df["vix_ma_20"].replace(0, np.nan)  # > 1 = elevated fear

        # VIX regime flags
        df["vix_low"] = (vix_aligned < 15).astype(int)  # complacent — risk-on
        df["vix_high"] = (vix_aligned > 25).astype(int)  # fear — risk-off
        df["vix_spike"] = (df["vix_relative"] > 1.3).astype(int)  # 30% above 20d MA
        return df

    def _add_moving_averages(self, df: pd.DataFrame, price: pd.Series) -> pd.DataFrame:
        for period in self.config.sma_periods:
            sma = price.rolling(period).mean()
            df[f"sma_{period}"] = sma
            df[f"price_vs_sma_{period}"] = (price - sma) / sma  # % distance from MA

        for period in self.config.ema_periods:
            ema = price.ewm(span=period, adjust=False).mean()
            df[f"ema_{period}"] = ema

        # MA crossover signals
        if 50 in self.config.sma_periods and 200 in self.config.sma_periods:
            df["golden_cross"] = (df["sma_50"] > df["sma_200"]).astype(int)

        # MA slope (trend direction)
        for period in self.config.sma_periods:
            df[f"sma_{period}_slope"] = df[f"sma_{period}"].pct_change(5)

        return df

    def _add_macd(self, df: pd.DataFrame, price: pd.Series) -> pd.DataFrame:
        ema_fast = price.ewm(span=self.config.macd_fast, adjust=False).mean()
        ema_slow = price.ewm(span=self.config.macd_slow, adjust=False).mean()

        df["macd_line"] = ema_fast - ema_slow
        df["macd_signal"] = df["macd_line"].ewm(span=self.config.macd_signal, adjust=False).mean()
        df["macd_histogram"] = df["macd_line"] - df["macd_signal"]
        df["macd_crossover"] = np.sign(df["macd_histogram"]).diff().fillna(0)
        return df

    def _add_rsi(self, df: pd.DataFrame, price: pd.Series) -> pd.DataFrame:
        delta = price.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.ewm(alpha=1 / self.config.rsi_period, min_periods=self.config.rsi_period).mean()
        avg_loss = loss.ewm(alpha=1 / self.config.rsi_period, min_periods=self.config.rsi_period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))
        df["rsi_ma"] = df["rsi"].rolling(10).mean()
        return df

    def _add_stochastic(self, df: pd.DataFrame) -> pd.DataFrame:
        period = self.config.stochastic_period
        low_min = df["Low"].rolling(period).min()
        high_max = df["High"].rolling(period).max()

        df["stoch_k"] = 100 * (df["Close"] - low_min) / (high_max - low_min).replace(0, np.nan)
        df["stoch_d"] = df["stoch_k"].rolling(3).mean()
        return df

    def _add_rate_of_change(self, df: pd.DataFrame, price: pd.Series) -> pd.DataFrame:
        for period in self.config.roc_periods:
            df[f"roc_{period}"] = price.pct_change(period) * 100
        return df

    def _add_bollinger_bands(self, df: pd.DataFrame, price: pd.Series) -> pd.DataFrame:
        period = self.config.bbands_period
        std_mult = self.config.bbands_std

        sma = price.rolling(period).mean()
        std = price.rolling(period).std()

        df["bb_upper"] = sma + std_mult * std
        df["bb_lower"] = sma - std_mult * std
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / sma
        df["bb_position"] = (price - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
        return df

    def _add_atr(self, df: pd.DataFrame) -> pd.DataFrame:
        high = df["High"]
        low = df["Low"]
        close = df["Close"].shift(1)

        tr1 = high - low
        tr2 = (high - close).abs()
        tr3 = (low - close).abs()

        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df["atr"] = true_range.ewm(span=self.config.atr_period, adjust=False).mean()
        df["atr_pct"] = df["atr"] / df["Close"]  # ATR as % of price
        return df

    def _add_volume_features(self, df: pd.DataFrame, price: pd.Series) -> pd.DataFrame:
        vol = df["Volume"]
        vol_ma = vol.rolling(self.config.volume_ma_period).mean()

        df["volume_ratio"] = vol / vol_ma.replace(0, np.nan)
        df["volume_ma"] = vol_ma

        # On-Balance Volume (OBV)
        direction = np.sign(price.diff())
        df["obv"] = (vol * direction).cumsum()
        df["obv_ma"] = df["obv"].rolling(20).mean()

        # Dollar volume
        df["dollar_volume"] = price * vol
        return df

    def _add_price_structure(self, df: pd.DataFrame, price: pd.Series) -> pd.DataFrame:
        # Distance from 52-week high/low
        high_252 = price.rolling(252, min_periods=60).max()
        low_252 = price.rolling(252, min_periods=60).min()

        df["dist_from_52w_high"] = (price - high_252) / high_252
        df["dist_from_52w_low"] = (price - low_252) / low_252.replace(0, np.nan)

        # Higher highs / lower lows (20-day)
        df["high_20"] = df["High"].rolling(20).max()
        df["low_20"] = df["Low"].rolling(20).min()
        df["breakout_up_20"] = (df["High"] >= df["high_20"].shift(1)).astype(int)
        df["breakout_down_20"] = (df["Low"] <= df["low_20"].shift(1)).astype(int)

        # Candle patterns (simple)
        body = df["Close"] - df["Open"]
        full_range = df["High"] - df["Low"]
        df["candle_body_pct"] = body / full_range.replace(0, np.nan)

        return df

    def get_feature_columns(self, df: pd.DataFrame) -> list[str]:
        """Return list of computed feature column names (excludes raw OHLCV and target)."""
        exclude_cols = {"Open", "High", "Low", "Close", "Adj Close", "Volume", "target", "ticker"}
        return [c for c in df.columns if c not in exclude_cols]

    def get_model_features(self, df: pd.DataFrame) -> list[str]:
        """Return feature columns suitable for ML model input."""
        exclude_patterns = [
            "sma_", "ema_", "bb_upper", "bb_lower", "atr",
            "high_20", "low_20", "obv", "volume_ma", "dollar_volume",
        ]
        # Keep ratio/normalized features, exclude raw price-level features
        features = []
        for col in self.get_feature_columns(df):
            if any(col == pat.rstrip("_") or (pat.endswith("_") and col.startswith(pat) and "vs" not in col and "slope" not in col and "ratio" not in col and "pct" not in col and "position" not in col)
                   for pat in exclude_patterns):
                continue
            features.append(col)
        return features
