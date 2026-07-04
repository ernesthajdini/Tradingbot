"""
Rank passing candidates by realized-vol percentile.

We DO NOT compute IV rank here. Implied volatility from free sources is
unreliable in ways a beginner can't detect. Instead we use realized volatility
percentile as a proxy: tickers whose recent realized vol is high relative to
their own 1-year history are more likely to have elevated IV worth selling.

This is intentionally a PROXY, not a substitute. The email will direct you to
check actual IV rank on barchart.com for the top names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from csp_screener import config


@dataclass
class RankedCandidate:
    ticker: str
    last_price: float
    rv_20d_annual: float        # current 20-day realized vol, annualized
    rv_percentile: float        # where it sits in 1-year history (0..100)
    avg_volume_20d: float
    next_earnings_days: Optional[int]
    rank: int = 0


def annualized_realized_vol(returns: pd.Series, window: int) -> pd.Series:
    """Annualized rolling realized vol of daily log returns."""
    # Daily log returns -> std -> *sqrt(252) for annual
    return returns.rolling(window).std() * np.sqrt(252)


def compute_rv_percentile(
    price_history: pd.Series,
    window: int = config.RV_WINDOW_DAYS,
    history_days: int = config.RV_HISTORY_DAYS,
) -> tuple[float, float]:
    """
    Returns (current_rv_20d_annual, percentile_0_to_100).

    Percentile is current RV's rank vs the trailing history_days of RV.
    100 = highest in the last year. 0 = lowest.
    """
    if price_history is None or len(price_history) < (window + 5):
        return (float("nan"), float("nan"))

    # Use log returns for vol estimation
    log_ret = np.log(price_history / price_history.shift(1)).dropna()
    if len(log_ret) < window + 5:
        return (float("nan"), float("nan"))

    rv_series = annualized_realized_vol(log_ret, window).dropna()
    if rv_series.empty:
        return (float("nan"), float("nan"))

    current = float(rv_series.iloc[-1])
    # Use up to history_days of prior values to rank against
    history = rv_series.iloc[-history_days:] if len(rv_series) >= history_days else rv_series
    if history.empty or history.max() == history.min():
        return (current, 50.0)

    pct = float((history <= current).sum() / len(history) * 100.0)
    return (current, pct)


def rank_candidates(
    candidates: list[dict],
    top_n: int = config.MAX_CANDIDATES_IN_EMAIL,
    ticker_scores: dict | None = None,
    current_vix: float | None = None,
) -> list[RankedCandidate]:
    """
    Rank passing candidates by realized vol percentile, optionally adjusted
    by learning-layer signals (ticker reliability + VIX regime weight).

    If ticker_scores is provided, the effective rank metric is:
      rv_percentile * ticker_score * vix_regime_weight
    where ticker_score is in [0.5, 1.2] from learning.ticker_scorer.
    """
    enriched = []
    for c in candidates:
        rv, pct = compute_rv_percentile(c.get("price_history"))
        if np.isnan(pct):
            continue
        enriched.append(
            RankedCandidate(
                ticker=c["ticker"],
                last_price=float(c["last_price"]),
                rv_20d_annual=rv,
                rv_percentile=pct,
                avg_volume_20d=float(c.get("avg_volume_20d") or 0.0),
                next_earnings_days=c.get("next_earnings_days"),
            )
        )

    # Compute effective rank metric incorporating learned ticker scores
    # (without mutating the displayed rv_percentile, which represents reality).
    if ticker_scores:
        from csp_screener.learning.confidence import _vix_regime_weight
        vix_w = _vix_regime_weight(current_vix)
        def rank_key(c):
            ts = ticker_scores.get(c.ticker)
            score = ts.score if (ts and ts.sample_quality != "thin") else 1.0
            return -(c.rv_percentile * score * vix_w)
        enriched.sort(key=lambda x: (rank_key(x), -x.rv_20d_annual))
    else:
        # Original behavior — pure RV percentile
        enriched.sort(key=lambda x: (-x.rv_percentile, -x.rv_20d_annual))

    for i, c in enumerate(enriched[:top_n], start=1):
        c.rank = i
    return enriched[:top_n]
