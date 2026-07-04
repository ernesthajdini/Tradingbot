"""
Feature bucket analysis.

For each closed trade, we record what feature buckets it was in at entry:
  - delta band (|delta| in [0, 0.15), [0.15, 0.25), [0.25, 0.35), [0.35, 0.5))
  - DTE band  (DTE in [25, 30), [30, 35), [35, 40), [40, 45))
  - RV percentile band (0-25, 25-50, 50-75, 75-100)
  - VIX regime  (<15, 15-20, 20-25, 25+)
  - data quality (ibkr_greeks, yfinance_iv_estimated_delta, premium_only)

Then for each (feature, bucket) pair, compute win rate and avg PnL.

This is the raw material for the recommender — it surfaces which buckets
are consistently winning vs losing, so you (or it) can re-bias future
selections.

Like the ticker scorer, we use Bayesian shrinkage so tiny buckets don't
generate spurious recommendations.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

PRIOR_TRADES = 5
PRIOR_WIN_RATE = 0.50

# Feature bucket definitions
DELTA_BANDS = [(0.0, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.50)]
DTE_BANDS = [(25, 30), (30, 35), (35, 40), (40, 46)]
RV_PCT_BANDS = [(0, 25), (25, 50), (50, 75), (75, 101)]
VIX_BANDS = [(0, 15), (15, 20), (20, 25), (25, 100)]


@dataclass
class BucketStats:
    feature: str
    bucket: str
    trades: int
    wins: int
    raw_win_rate: float
    shrunk_win_rate: float
    avg_pnl: float
    total_pnl: float
    deviation_from_overall: float   # how far this bucket diverges from baseline


def _bucket_label(value: float, bands: list[tuple]) -> Optional[str]:
    for lo, hi in bands:
        if lo <= abs(value) < hi:
            return f"{lo:g}-{hi:g}"
    return None


def _classify_trade(trade: dict, screen_payload: Optional[dict] = None) -> dict:
    """Map a closed trade to its feature buckets."""
    buckets = {}

    delta = trade.get("delta_at_open")
    if delta is not None:
        b = _bucket_label(delta, DELTA_BANDS)
        if b:
            buckets["delta"] = b

    dte = trade.get("dte_at_open")
    if dte is not None:
        b = _bucket_label(float(dte), DTE_BANDS)
        if b:
            buckets["dte"] = b

    # RV percentile + VIX come from the screen payload at the time
    # (if available). We accept them as kwargs to be flexible.
    rv = trade.get("rv_percentile_at_open")
    if rv is None and screen_payload:
        # Try to find this trade's candidate in the screen payload
        for c in (screen_payload.get("candidates_payload") or []):
            if c.get("ticker") == trade.get("ticker"):
                rv = c.get("rv_percentile")
                break
    if rv is not None:
        b = _bucket_label(float(rv), RV_PCT_BANDS)
        if b:
            buckets["rv_percentile"] = b

    vix = trade.get("vix_at_open")
    if vix is None and screen_payload:
        vix = screen_payload.get("vix")
    if vix is not None:
        b = _bucket_label(float(vix), VIX_BANDS)
        if b:
            buckets["vix"] = b

    dq = trade.get("data_quality")
    if dq:
        buckets["data_quality"] = dq

    return buckets


def analyze_features(
    closed_trades: list[dict],
    screens_by_id: Optional[dict[str, dict]] = None,
) -> list[BucketStats]:
    """
    Return a list of BucketStats — one per (feature, bucket) pair.

    closed_trades: list of dicts with at least pnl, ticker, screen_id,
                   and optional delta_at_open / dte_at_open / etc.
    screens_by_id: optional map of screen_id -> screen record. Used to
                   look up rv_percentile / vix when the trade row doesn't
                   carry them.
    """
    if not closed_trades:
        return []

    overall_pnls = [float(t.get("pnl") or 0) for t in closed_trades]
    overall_wins = sum(1 for p in overall_pnls if p > 0)
    overall_win_rate = overall_wins / len(overall_pnls)

    # Bucket aggregation
    buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

    for trade in closed_trades:
        screen_payload = None
        if screens_by_id:
            screen_payload = screens_by_id.get(trade.get("screen_id"))
        labels = _classify_trade(trade, screen_payload)
        pnl = float(trade.get("pnl") or 0)
        win = pnl > 0
        for feature, bucket in labels.items():
            key = (feature, bucket)
            buckets[key]["trades"] += 1
            if win:
                buckets[key]["wins"] += 1
            buckets[key]["pnl"] += pnl

    # Build stats
    out: list[BucketStats] = []
    for (feature, bucket), agg in buckets.items():
        n = agg["trades"]
        wins = agg["wins"]
        pnl = agg["pnl"]
        raw_wr = wins / n if n else 0.0
        shrunk_wr = (wins + PRIOR_TRADES * PRIOR_WIN_RATE) / (n + PRIOR_TRADES)
        avg_pnl = pnl / n if n else 0.0
        out.append(BucketStats(
            feature=feature,
            bucket=bucket,
            trades=n,
            wins=wins,
            raw_win_rate=round(raw_wr, 4),
            shrunk_win_rate=round(shrunk_wr, 4),
            avg_pnl=round(avg_pnl, 2),
            total_pnl=round(pnl, 2),
            deviation_from_overall=round(shrunk_wr - overall_win_rate, 4),
        ))

    # Sort by (feature, abs(deviation)) so the most-divergent buckets float
    out.sort(key=lambda b: (b.feature, -abs(b.deviation_from_overall)))
    return out


def overall_baseline(closed_trades: list[dict]) -> dict:
    """Compute the overall baseline (used for deviation context)."""
    if not closed_trades:
        return {"trades": 0, "win_rate": 0.5, "avg_pnl": 0.0}
    pnls = [float(t.get("pnl") or 0) for t in closed_trades]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "trades": len(closed_trades),
        "win_rate": round(wins / len(closed_trades), 4),
        "avg_pnl": round(sum(pnls) / len(closed_trades), 2),
    }
