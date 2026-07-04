"""
Per-ticker reliability scoring.

For each ticker that's been through the virtual portfolio, compute a score
in [0, 1] based on:
  - Recent win rate (with statistical confidence — small samples shrink toward 0.5)
  - Recent average PnL (rewards consistent winners, penalizes blow-ups)
  - Sample size (very small samples cap effect)

The score is used by the ranker as a MULTIPLIER on the candidate's RV
percentile rank. Score 1.0 = unchanged ranking. Score 0.5 = deprioritized
(treated as if RV percentile were halved). Score 1.2 = slight boost.

Range: [MIN_SCORE=0.5, MAX_SCORE=1.2].
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Bayesian shrinkage prior — pretend every ticker starts with 5 "neutral"
# trades (win rate 50%). This means very small samples shrink hard toward
# the average and only large samples move the score meaningfully.
PRIOR_TRADES = 5
PRIOR_WIN_RATE = 0.50

MIN_SCORE = 0.5    # chronic losers never go below 50% weight
MAX_SCORE = 1.2    # winners get at most 20% boost
COOLDOWN_TRADES = 3  # need at least this many trades before any adjustment

# Recency: only consider closed trades within this many days
LOOKBACK_DAYS = 180


@dataclass
class TickerStats:
    ticker: str
    trades: int
    wins: int
    losses: int
    raw_win_rate: float          # observed win rate (no shrinkage)
    shrunk_win_rate: float       # Bayesian-shrunk toward 0.5
    avg_pnl: float
    total_pnl: float
    score: float                 # ranking multiplier [MIN_SCORE, MAX_SCORE]
    sample_quality: str          # "thin" | "moderate" | "robust"


def score_ticker(
    ticker: str,
    closed_trades: list[dict],
    now: Optional[datetime] = None,
) -> TickerStats:
    """
    Score one ticker from its closed virtual trades.

    closed_trades: list of dicts each with at least:
      - ticker (must match)
      - pnl (float)
      - closed_at (ISO datetime str)
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    # Filter to this ticker, recent only
    relevant = []
    for t in closed_trades:
        if t.get("ticker") != ticker:
            continue
        closed_at = t.get("closed_at")
        if not closed_at:
            continue
        try:
            ts = datetime.fromisoformat(closed_at.replace("Z", "+00:00")).replace(tzinfo=None)
            if ts >= cutoff:
                relevant.append(t)
        except Exception:
            continue

    if not relevant:
        return TickerStats(
            ticker=ticker, trades=0, wins=0, losses=0,
            raw_win_rate=0.0, shrunk_win_rate=PRIOR_WIN_RATE,
            avg_pnl=0.0, total_pnl=0.0,
            score=1.0, sample_quality="thin",
        )

    pnls = [float(t.get("pnl") or 0) for t in relevant]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    n = len(pnls)

    raw_win_rate = wins / n if n else 0.0

    # Bayesian shrinkage: blend with prior weighted by PRIOR_TRADES
    shrunk_win_rate = (wins + PRIOR_TRADES * PRIOR_WIN_RATE) / (n + PRIOR_TRADES)

    total_pnl = sum(pnls)
    avg_pnl = total_pnl / n

    # Sample quality buckets
    if n < COOLDOWN_TRADES:
        sample_quality = "thin"
    elif n < 10:
        sample_quality = "moderate"
    else:
        sample_quality = "robust"

    # If sample too thin, neutral score (let the candidate be ranked by RV alone)
    if n < COOLDOWN_TRADES:
        score = 1.0
    else:
        # Center the shrunk win rate around 0.5 → map to [MIN_SCORE, MAX_SCORE]
        # 0.5 win rate -> 1.0
        # 0.65 win rate (good) -> ~1.15
        # 0.35 win rate (bad)  -> ~0.7
        # Use linear mapping with bounds
        delta_from_neutral = shrunk_win_rate - 0.5  # in [-0.5, 0.5]
        # Scale so that ±0.15 from neutral hits ±0.2 score change
        score = 1.0 + (delta_from_neutral / 0.5) * 0.2
        # Penalize negative average PnL more than just win rate suggests
        if avg_pnl < 0 and n >= COOLDOWN_TRADES:
            score *= 0.9
        score = max(MIN_SCORE, min(MAX_SCORE, score))

    return TickerStats(
        ticker=ticker,
        trades=n,
        wins=wins,
        losses=losses,
        raw_win_rate=round(raw_win_rate, 4),
        shrunk_win_rate=round(shrunk_win_rate, 4),
        avg_pnl=round(avg_pnl, 2),
        total_pnl=round(total_pnl, 2),
        score=round(score, 3),
        sample_quality=sample_quality,
    )


def score_all_tickers(closed_trades: list[dict]) -> dict[str, TickerStats]:
    """Score every ticker that appears in the closed_trades list."""
    tickers = {t.get("ticker") for t in closed_trades if t.get("ticker")}
    return {tk: score_ticker(tk, closed_trades) for tk in tickers}


def get_ticker_score(ticker: str, all_scores: dict[str, TickerStats]) -> float:
    """Convenience lookup. Returns 1.0 for tickers with no history."""
    stats = all_scores.get(ticker)
    if not stats or stats.sample_quality == "thin":
        return 1.0
    return stats.score
