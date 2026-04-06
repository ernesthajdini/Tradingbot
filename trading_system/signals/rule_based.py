"""
Rule-based signal generation.
Implements classic technical analysis strategies as quantifiable rules.
"""

import numpy as np
import pandas as pd

from trading_system.config.settings import SignalConfig


class RuleBasedSignals:
    """Generate trading signals from technical rules."""

    def __init__(self, config: SignalConfig | None = None):
        self.config = config or SignalConfig()

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate rule-based signals from a feature-enriched DataFrame.
        Returns DataFrame with signal columns added.
        """
        signals = pd.DataFrame(index=df.index)

        # Individual strategy signals (-1 to +1 scale)
        signals["sig_ma_crossover"] = self._ma_crossover(df)
        signals["sig_rsi_reversal"] = self._rsi_reversal(df)
        signals["sig_macd_momentum"] = self._macd_momentum(df)
        signals["sig_bollinger_mean_revert"] = self._bollinger_mean_reversion(df)
        signals["sig_volume_breakout"] = self._volume_breakout(df)
        signals["sig_trend_following"] = self._trend_following(df)

        # Composite score: weighted average of all strategies
        weights = {
            "sig_ma_crossover": 0.20,
            "sig_rsi_reversal": 0.15,
            "sig_macd_momentum": 0.20,
            "sig_bollinger_mean_revert": 0.15,
            "sig_volume_breakout": 0.10,
            "sig_trend_following": 0.20,
        }

        composite = sum(signals[col] * w for col, w in weights.items())
        signals["rule_score"] = composite

        # Convert to direction
        signals["rule_direction"] = np.where(
            composite > 0.15, "BUY",
            np.where(composite < -0.15, "SELL", "HOLD")
        )

        # Confidence: absolute value of score, scaled to 0-1
        signals["rule_confidence"] = np.clip(composite.abs() * 2, 0, 1)

        # Reasoning
        signals["rule_reasons"] = self._build_reasoning(signals, df)

        return signals

    def _ma_crossover(self, df: pd.DataFrame) -> pd.Series:
        """
        Moving average crossover strategy.
        Bullish: price > SMA50 > SMA200
        Bearish: price < SMA50 < SMA200
        """
        signal = pd.Series(0.0, index=df.index)

        if "sma_50" not in df.columns or "sma_200" not in df.columns:
            return signal

        price = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]

        # Trend alignment
        above_50 = price > df["sma_50"]
        above_200 = price > df["sma_200"]
        ma50_above_200 = df["sma_50"] > df["sma_200"]

        # Full bullish alignment
        signal[above_50 & above_200 & ma50_above_200] = 0.5

        # Golden cross (recent)
        if "golden_cross" in df.columns:
            golden_now = df["golden_cross"] == 1
            golden_prev = df["golden_cross"].shift(5) == 0
            signal[golden_now & golden_prev] += 0.5  # boost for fresh cross

        # Full bearish alignment
        signal[~above_50 & ~above_200 & ~ma50_above_200] = -0.5

        # Death cross
        if "golden_cross" in df.columns:
            death_now = df["golden_cross"] == 0
            death_prev = df["golden_cross"].shift(5) == 1
            signal[death_now & death_prev] -= 0.5

        return signal.clip(-1, 1)

    def _rsi_reversal(self, df: pd.DataFrame) -> pd.Series:
        """
        RSI mean-reversion strategy.
        Buy when RSI is oversold and turning up.
        Sell when RSI is overbought and turning down.
        """
        signal = pd.Series(0.0, index=df.index)
        if "rsi" not in df.columns:
            return signal

        rsi = df["rsi"]
        rsi_rising = rsi > rsi.shift(1)
        rsi_falling = rsi < rsi.shift(1)

        # Oversold reversal
        oversold = rsi < self.config.rsi_oversold
        signal[oversold & rsi_rising] = 0.7
        signal[oversold & ~rsi_rising] = 0.3

        # Overbought reversal
        overbought = rsi > self.config.rsi_overbought
        signal[overbought & rsi_falling] = -0.7
        signal[overbought & ~rsi_falling] = -0.3

        return signal

    def _macd_momentum(self, df: pd.DataFrame) -> pd.Series:
        """MACD crossover and histogram momentum."""
        signal = pd.Series(0.0, index=df.index)
        if "macd_histogram" not in df.columns:
            return signal

        hist = df["macd_histogram"]
        hist_rising = hist > hist.shift(1)
        hist_positive = hist > 0

        # Bullish: histogram positive and rising
        signal[hist_positive & hist_rising] = 0.6
        signal[hist_positive & ~hist_rising] = 0.2

        # Bearish: histogram negative and falling
        signal[~hist_positive & ~hist_rising] = -0.6
        signal[~hist_positive & hist_rising] = -0.2

        # Fresh crossover is strongest signal
        if "macd_crossover" in df.columns:
            signal[df["macd_crossover"] > 0] = 0.8   # bullish cross
            signal[df["macd_crossover"] < 0] = -0.8  # bearish cross

        return signal

    def _bollinger_mean_reversion(self, df: pd.DataFrame) -> pd.Series:
        """Bollinger Band mean reversion: buy at lower band, sell at upper."""
        signal = pd.Series(0.0, index=df.index)
        if "bb_position" not in df.columns:
            return signal

        pos = df["bb_position"]

        # Price near lower band (oversold)
        signal[pos < 0.05] = 0.7
        signal[(pos >= 0.05) & (pos < 0.2)] = 0.3

        # Price near upper band (overbought)
        signal[pos > 0.95] = -0.7
        signal[(pos <= 0.95) & (pos > 0.8)] = -0.3

        return signal

    def _volume_breakout(self, df: pd.DataFrame) -> pd.Series:
        """High-volume breakout confirmation."""
        signal = pd.Series(0.0, index=df.index)
        if "volume_ratio" not in df.columns:
            return signal

        price = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
        vol_surge = df["volume_ratio"] > self.config.volume_surge_threshold
        price_up = price.pct_change() > 0.01
        price_down = price.pct_change() < -0.01

        # High volume + price up = bullish breakout
        signal[vol_surge & price_up] = 0.7

        # High volume + price down = bearish breakdown
        signal[vol_surge & price_down] = -0.7

        return signal

    def _trend_following(self, df: pd.DataFrame) -> pd.Series:
        """Trend following based on price structure and momentum."""
        signal = pd.Series(0.0, index=df.index)

        price = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]

        # Multi-timeframe momentum alignment
        components = []

        if "return_5d" in df.columns:
            components.append(np.sign(df["return_5d"]) * 0.3)
        if "return_20d" in df.columns:
            components.append(np.sign(df["return_20d"]) * 0.3)
        if "price_vs_sma_50" in df.columns:
            components.append(np.sign(df["price_vs_sma_50"]) * 0.4)

        if components:
            signal = sum(components)

        # Breakout boost
        if "breakout_up_20" in df.columns:
            signal[df["breakout_up_20"] == 1] += 0.2
        if "breakout_down_20" in df.columns:
            signal[df["breakout_down_20"] == 1] -= 0.2

        return signal.clip(-1, 1)

    def _build_reasoning(self, signals: pd.DataFrame, df: pd.DataFrame) -> pd.Series:
        """Build human-readable reasoning for each signal."""
        reasons = pd.Series("", index=df.index, dtype=object)

        for idx in df.index:
            parts = []
            row = signals.loc[idx]

            if abs(row.get("sig_ma_crossover", 0)) > 0.3:
                direction = "bullish" if row["sig_ma_crossover"] > 0 else "bearish"
                parts.append(f"MA alignment is {direction}")

            if abs(row.get("sig_rsi_reversal", 0)) > 0.3:
                rsi_val = df.loc[idx, "rsi"] if "rsi" in df.columns else "?"
                if row["sig_rsi_reversal"] > 0:
                    parts.append(f"RSI oversold ({rsi_val:.0f})" if isinstance(rsi_val, float) else "RSI oversold")
                else:
                    parts.append(f"RSI overbought ({rsi_val:.0f})" if isinstance(rsi_val, float) else "RSI overbought")

            if abs(row.get("sig_macd_momentum", 0)) > 0.3:
                direction = "bullish" if row["sig_macd_momentum"] > 0 else "bearish"
                parts.append(f"MACD {direction} momentum")

            if abs(row.get("sig_volume_breakout", 0)) > 0.3:
                direction = "up" if row["sig_volume_breakout"] > 0 else "down"
                parts.append(f"Volume breakout {direction}")

            reasons[idx] = "; ".join(parts) if parts else "No strong signals"

        return reasons
