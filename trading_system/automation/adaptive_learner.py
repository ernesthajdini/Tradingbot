"""
Adaptive Learning Engine.

This is the brain that makes the system actually learn from experience.
It analyzes past trade outcomes and adjusts:
  1. Strategy weights (which signals to trust more/less)
  2. Confidence thresholds (what minimum confidence to act on)
  3. Ticker preferences (which stocks are most predictable)
  4. Regime awareness (what works in bull vs bear vs sideways markets)

Runs after every trade closes and during weekly retrain.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np

from trading_system.automation.trade_journal import TradeJournal

logger = logging.getLogger(__name__)


class AdaptiveLearner:
    """Learns from trade outcomes and adapts strategy parameters."""

    def __init__(self, journal: TradeJournal | None = None):
        self.journal = journal or TradeJournal()
        self.weights_file = Path("trading_system/output/learned_weights.json")
        self.weights = self._load_weights()

    def _load_weights(self) -> dict:
        """Load learned weights from disk."""
        if self.weights_file.exists():
            try:
                return json.loads(self.weights_file.read_text())
            except Exception:
                pass
        # Defaults
        return {
            "strategy_weights": {
                "sig_ma_crossover": 0.20,
                "sig_rsi_reversal": 0.15,
                "sig_macd_momentum": 0.20,
                "sig_bollinger_mean_revert": 0.15,
                "sig_volume_breakout": 0.10,
                "sig_trend_following": 0.20,
            },
            "min_confidence": 0.30,
            "ticker_scores": {},
            "regime": "unknown",
            "last_updated": None,
            "learning_iterations": 0,
            "trade_history_analyzed": 0,
        }

    def _save_weights(self):
        """Persist learned weights."""
        self.weights["last_updated"] = datetime.now().isoformat()
        self.weights_file.parent.mkdir(parents=True, exist_ok=True)
        self.weights_file.write_text(json.dumps(self.weights, indent=2))

    def learn(self) -> dict:
        """
        Main learning loop. Analyzes all closed trades and adapts weights.
        Call this after trades close and during weekly retrain.
        Returns summary of what was learned.
        """
        closed_trades = self.journal.get_closed_trades()
        if len(closed_trades) < 5:
            return {
                "status": "insufficient_data",
                "trades_needed": 5 - len(closed_trades),
                "message": f"Need at least 5 closed trades to learn. Have {len(closed_trades)}."
            }

        lessons = {}

        # 1. Learn strategy weights from outcomes
        strategy_lesson = self._learn_strategy_weights()
        if strategy_lesson:
            lessons["strategy_weights"] = strategy_lesson

        # 2. Learn optimal confidence threshold
        confidence_lesson = self._learn_confidence_threshold()
        if confidence_lesson:
            lessons["confidence"] = confidence_lesson

        # 3. Learn ticker predictability
        ticker_lesson = self._learn_ticker_scores()
        if ticker_lesson:
            lessons["tickers"] = ticker_lesson

        # 4. Detect market regime
        regime_lesson = self._detect_regime()
        if regime_lesson:
            lessons["regime"] = regime_lesson

        # Update metadata
        self.weights["learning_iterations"] = self.weights.get("learning_iterations", 0) + 1
        self.weights["trade_history_analyzed"] = len(closed_trades)

        self._save_weights()
        logger.info(f"Learning complete. Iteration #{self.weights['learning_iterations']}")

        return {
            "status": "learned",
            "iteration": self.weights["learning_iterations"],
            "trades_analyzed": len(closed_trades),
            "lessons": lessons,
        }

    def _learn_strategy_weights(self) -> dict | None:
        """
        Analyze which sub-signals predicted winning vs losing trades.
        Increase weight for strategies that predict winners, decrease for losers.
        """
        accuracy = self.journal.get_strategy_accuracy()
        if isinstance(accuracy, dict) and "message" in accuracy:
            return None

        if not accuracy:
            return None

        old_weights = dict(self.weights["strategy_weights"])
        new_weights = dict(old_weights)

        adjustments = {}
        for strategy_key, stats in accuracy.items():
            if stats["total_trades"] < 3:
                continue

            # Map journal strategy names to signal column names
            signal_key = f"sig_{strategy_key}" if not strategy_key.startswith("sig_") else strategy_key

            if signal_key not in new_weights:
                # Try to match rule_based or ml_model
                if strategy_key == "rule_based":
                    continue  # composite, not a single strategy
                if strategy_key == "ml_model":
                    continue  # handled separately
                continue

            win_rate = stats["win_rate"]

            # Adjust weight: increase for winners, decrease for losers
            # Use a conservative learning rate to avoid overcorrecting
            if win_rate > 0.55:
                adjustment = 0.02  # small boost
            elif win_rate < 0.40:
                adjustment = -0.02  # small penalty
            else:
                adjustment = 0.0  # neutral

            new_weights[signal_key] = max(0.05, min(0.40, new_weights[signal_key] + adjustment))
            adjustments[signal_key] = {
                "win_rate": round(win_rate, 3),
                "old_weight": round(old_weights.get(signal_key, 0), 3),
                "new_weight": round(new_weights[signal_key], 3),
                "adjustment": adjustment,
            }

        # Normalize weights to sum to 1.0
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: round(v / total, 3) for k, v in new_weights.items()}

        self.weights["strategy_weights"] = new_weights
        return adjustments if adjustments else None

    def _learn_confidence_threshold(self) -> dict | None:
        """
        Find the optimal minimum confidence threshold.
        Test different thresholds and find where win rate is maximized.
        """
        closed = self.journal.get_closed_trades()
        if len(closed) < 10:
            return None

        # Get signals linked to trades
        signals = self.journal.get_signal_history(days=90)
        signal_map = {s["id"]: s for s in signals}

        # Analyze win rate at different confidence levels
        thresholds = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
        best_threshold = 0.30
        best_score = 0

        results = {}
        for thresh in thresholds:
            trades_above = [
                t for t in closed
                if t.get("signal_id") and
                signal_map.get(t["signal_id"], {}).get("confidence", 0) >= thresh
            ]

            if len(trades_above) < 3:
                continue

            wins = sum(1 for t in trades_above if (t.get("pnl", 0) or 0) > 0)
            win_rate = wins / len(trades_above)
            avg_pnl = sum(t.get("pnl_pct", 0) or 0 for t in trades_above) / len(trades_above)

            # Score = win_rate * (1 + avg_pnl) — rewards both consistency and profitability
            score = win_rate * (1 + avg_pnl)

            results[str(thresh)] = {
                "win_rate": round(win_rate, 3),
                "avg_pnl": round(avg_pnl, 4),
                "trades": len(trades_above),
                "score": round(score, 4),
            }

            if score > best_score:
                best_score = score
                best_threshold = thresh

        old_threshold = self.weights.get("min_confidence", 0.30)
        # Move slowly toward optimal — don't jump
        new_threshold = round(old_threshold * 0.7 + best_threshold * 0.3, 2)
        self.weights["min_confidence"] = new_threshold

        return {
            "old_threshold": old_threshold,
            "optimal_threshold": best_threshold,
            "new_threshold": new_threshold,
            "analysis": results,
        }

    def _learn_ticker_scores(self) -> dict | None:
        """
        Score each ticker by how predictable/profitable it is.
        Higher score = our system is better at trading this ticker.
        """
        closed = self.journal.get_closed_trades()
        if len(closed) < 5:
            return None

        ticker_stats = {}
        for trade in closed:
            ticker = trade["ticker"]
            if ticker not in ticker_stats:
                ticker_stats[ticker] = {"wins": 0, "losses": 0, "total_pnl": 0}

            pnl = trade.get("pnl_pct", 0) or 0
            if pnl > 0:
                ticker_stats[ticker]["wins"] += 1
            else:
                ticker_stats[ticker]["losses"] += 1
            ticker_stats[ticker]["total_pnl"] += pnl

        ticker_scores = {}
        for ticker, stats in ticker_stats.items():
            total = stats["wins"] + stats["losses"]
            if total < 2:
                continue
            win_rate = stats["wins"] / total
            avg_pnl = stats["total_pnl"] / total
            # Score: combination of win rate and avg return
            score = round((win_rate * 0.6 + (0.5 + avg_pnl * 10) * 0.4), 3)
            ticker_scores[ticker] = {
                "score": score,
                "win_rate": round(win_rate, 3),
                "avg_pnl": round(avg_pnl, 4),
                "total_trades": total,
            }

        self.weights["ticker_scores"] = {
            t: s["score"] for t, s in ticker_scores.items()
        }
        return ticker_scores if ticker_scores else None

    def _detect_regime(self) -> dict | None:
        """
        Detect current market regime based on recent trade patterns.
        Adjusts behavior: conservative in volatile/bear, aggressive in bull.
        """
        closed = self.journal.get_closed_trades(days=30)
        if len(closed) < 5:
            return None

        pnls = [t.get("pnl_pct", 0) or 0 for t in closed]
        avg_pnl = np.mean(pnls)
        pnl_std = np.std(pnls)
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)

        if avg_pnl > 0.02 and win_rate > 0.55:
            regime = "bullish"
        elif avg_pnl < -0.02 and win_rate < 0.45:
            regime = "bearish"
        elif pnl_std > 0.05:
            regime = "volatile"
        else:
            regime = "sideways"

        old_regime = self.weights.get("regime", "unknown")
        self.weights["regime"] = regime

        # Adjust behavior per regime
        regime_adjustments = {
            "bullish": {"bias": "favor longs", "position_scale": 1.0},
            "bearish": {"bias": "favor shorts, reduce size", "position_scale": 0.5},
            "volatile": {"bias": "widen stops, reduce size", "position_scale": 0.5},
            "sideways": {"bias": "mean reversion, tight stops", "position_scale": 0.75},
        }

        return {
            "old_regime": old_regime,
            "new_regime": regime,
            "avg_pnl_30d": round(avg_pnl, 4),
            "volatility_30d": round(pnl_std, 4),
            "win_rate_30d": round(win_rate, 3),
            "adjustments": regime_adjustments.get(regime, {}),
        }

    def get_learned_weights(self) -> dict:
        """Return current learned parameters for use by signal generator."""
        return self.weights

    def summary(self) -> str:
        """Human-readable summary of what has been learned."""
        w = self.weights
        lines = [
            f"\n{'='*55}",
            f"  ADAPTIVE LEARNING STATUS",
            f"{'='*55}",
            f"  Learning iterations:  {w.get('learning_iterations', 0)}",
            f"  Trades analyzed:      {w.get('trade_history_analyzed', 0)}",
            f"  Last updated:         {w.get('last_updated', 'never')}",
            f"  Market regime:        {w.get('regime', 'unknown')}",
            f"  Min confidence:       {w.get('min_confidence', 0.3):.2f}",
            f"\n  Strategy Weights:",
        ]
        for strategy, weight in w.get("strategy_weights", {}).items():
            bar = "#" * int(weight * 50)
            name = strategy.replace("sig_", "")
            lines.append(f"    {name:25s} {weight:.3f} {bar}")

        if w.get("ticker_scores"):
            lines.append(f"\n  Top Tickers (by predictability):")
            sorted_tickers = sorted(
                w["ticker_scores"].items(), key=lambda x: x[1], reverse=True
            )
            for ticker, score in sorted_tickers[:10]:
                lines.append(f"    {ticker:8s} score={score:.3f}")

        lines.append(f"{'='*55}\n")
        return "\n".join(lines)
