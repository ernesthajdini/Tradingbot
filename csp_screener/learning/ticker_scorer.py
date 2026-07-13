"""
Per-ticker reliability scoring.

For each ticker that's been through the virtual portfolio, compute a
ranking-multiplier score based on:
  - Shrunk EXPECTANCY (avg PnL as % of credit received) — the primary
    signal. Premium selling has asymmetric payoffs: many small wins, rare
    large losses. A ticker can win 80% of the time and still lose money,
    so win rate alone is the wrong scoreboard.
  - Shrunk win rate — secondary, half-weighted.
  - Sample size — Bayesian shrinkage toward neutral; below COOLDOWN_TRADES
    the score stays exactly 1.0, and small samples past it move the score
    only modestly (blow-up losses excepted — deliberately).

Trades lacking credit data (very old records) fall back to win-rate-only
scoring, so the scorer degrades rather than breaks on legacy journals.

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

# Expectancy weighting. shrunk_expectancy is denominated in units of credit
# received (a typical 50%-take-profit win ≈ +0.45 after friction; a 2x-credit
# stop-loss ≈ -2.0), then shrunk by the same 5-phantom-trade prior (at 0).
#
# Calibration (EXPECTANCY_SCALE=0.5): reaching MAX requires a SUSTAINED
# record — 10 straight +0.45 wins gives shrunk_exp 0.30 → +0.15 exp + 0.07
# wr ≈ MAX. Three trades move the score only modestly (3 wins → ~1.12,
# 3 modest losses → ~0.91), never to a clamp — EXCEPT blow-up losses
# (3 × -2x credit → ~0.63), which is deliberate: catastrophic losses are
# exactly what must be penalized fast. A hotter scale saturated the clamps
# at n=3 and voided the shrinkage guarantee (caught in adversarial review).
#
# The win-rate secondary is only added when expectancy is POSITIVE. When
# expectancy is negative, win rate is ignored entirely — otherwise a high
# win rate could drag a money-losing ticker above neutral (the 8-small-wins/
# 2-blow-ups trap this rewrite exists to catch). Pinned by
# test_high_win_rate_but_negative_ev_scores_below_neutral and
# test_mildly_negative_ev_never_scores_above_neutral.
EXPECTANCY_SCALE = 0.5
WINRATE_SECONDARY_SCALE = 0.2  # ±0.5 of win rate moves the score only ±0.1


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
    avg_pnl_pct: float = 0.0     # avg PnL as fraction of credit (0 if unknown)
    shrunk_expectancy: float = 0.0  # Bayesian-shrunk avg_pnl_pct


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

    # PnL as a fraction of credit received — the expectancy signal. Falls
    # back to pnl/credit for records predating pnl_pct_of_credit; skips
    # trades with neither (legacy journals).
    pnl_pcts: list[float] = []
    for t in relevant:
        v = t.get("pnl_pct_of_credit")
        if v is None:
            credit = t.get("credit_received")
            try:
                if credit and float(credit) > 0 and t.get("pnl") is not None:
                    v = float(t["pnl"]) / float(credit)
            except (ValueError, TypeError):
                v = None
        if v is not None:
            try:
                pnl_pcts.append(float(v))
            except (ValueError, TypeError):
                pass

    avg_pnl_pct = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0
    # Shrink toward 0 with the same phantom-trade prior (prior contributes 0)
    shrunk_expectancy = (
        sum(pnl_pcts) / (len(pnl_pcts) + PRIOR_TRADES) if pnl_pcts else 0.0
    )

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
    elif len(pnl_pcts) >= COOLDOWN_TRADES:
        # Expectancy-primary scoring: dollars (per credit) drive the score.
        # Win rate only ever ADDS on top of positive expectancy — a
        # negative-expectancy ticker can never be pushed above neutral by
        # a pretty win-rate streak.
        exp_component = shrunk_expectancy * EXPECTANCY_SCALE
        if shrunk_expectancy > 0:
            wr_component = (shrunk_win_rate - 0.5) * WINRATE_SECONDARY_SCALE
            score = 1.0 + exp_component + wr_component
        else:
            score = 1.0 + exp_component
        score = max(MIN_SCORE, min(MAX_SCORE, score))
    else:
        # Legacy fallback: no credit data — original win-rate-only mapping
        delta_from_neutral = shrunk_win_rate - 0.5  # in [-0.5, 0.5]
        score = 1.0 + (delta_from_neutral / 0.5) * 0.2
        if avg_pnl < 0:
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
        avg_pnl_pct=round(avg_pnl_pct, 4),
        shrunk_expectancy=round(shrunk_expectancy, 4),
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
