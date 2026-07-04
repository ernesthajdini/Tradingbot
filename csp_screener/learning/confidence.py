"""
Per-candidate confidence score.

Combines:
  - RV percentile (the base "is the IV elevated" signal)
  - Ticker historical score (from ticker_scorer)
  - Data quality (penalize estimated-greeks setups)
  - Current VIX regime fit (lower selectivity in calm markets)

Returns a score in [0, 1] roughly equal to: "how much should we trust
this candidate beyond what a naive RV-ranking would suggest."

The ranker multiplies the raw RV percentile by this score, so a 90th
percentile candidate with confidence 0.6 ranks below an 80th percentile
candidate with confidence 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from csp_screener.learning.ticker_scorer import TickerStats

DATA_QUALITY_WEIGHTS = {
    "ibkr_greeks": 1.0,
    "yfinance_iv_estimated_delta": 0.85,
    "premium_only_no_greeks": 0.6,
    None: 0.7,
}


@dataclass
class CandidateConfidence:
    ticker: str
    rv_percentile: float
    ticker_score: float
    data_quality_weight: float
    vix_regime_weight: float
    composite: float          # final multiplier in [0, 1.5]
    components: dict


def _vix_regime_weight(vix: Optional[float]) -> float:
    """
    Weight by VIX regime. We want to be MORE selective in calm markets
    (where premium is low and not worth the assignment risk) and slightly
    LESS selective in moderately elevated markets (premium is fat).

    VIX < 12  → 0.85 (too calm to be worth it)
    VIX 12-20 → 1.0  (normal)
    VIX 20-28 → 1.1  (premium fat, slight boost)
    VIX 28-35 → 0.95 (elevated but still tradeable)
    VIX > 35  → handled by kill switch in config
    """
    if vix is None:
        return 1.0
    if vix < 12:
        return 0.85
    if vix < 20:
        return 1.0
    if vix < 28:
        return 1.1
    if vix < 35:
        return 0.95
    return 0.6  # extreme — defer to kill switch


def compute_confidence(
    ticker: str,
    rv_percentile: float,
    data_quality: Optional[str],
    ticker_stats: dict[str, TickerStats],
    current_vix: Optional[float] = None,
) -> CandidateConfidence:
    """
    Compute the composite confidence multiplier for a candidate.
    """
    stats = ticker_stats.get(ticker)
    ticker_score = stats.score if (stats and stats.sample_quality != "thin") else 1.0

    dq_weight = DATA_QUALITY_WEIGHTS.get(data_quality, 0.7)
    vix_weight = _vix_regime_weight(current_vix)

    composite = ticker_score * dq_weight * vix_weight
    # Clamp to a reasonable band
    composite = max(0.3, min(1.5, composite))

    return CandidateConfidence(
        ticker=ticker,
        rv_percentile=rv_percentile,
        ticker_score=ticker_score,
        data_quality_weight=dq_weight,
        vix_regime_weight=vix_weight,
        composite=round(composite, 3),
        components={
            "ticker_score": round(ticker_score, 3),
            "data_quality": data_quality or "unknown",
            "data_quality_weight": round(dq_weight, 3),
            "vix": current_vix,
            "vix_weight": round(vix_weight, 3),
        },
    )
