"""
Recommender — turns feature_analyzer output into plain-English suggestions.

CRITICAL DESIGN RULE: This module returns text. It NEVER writes to
config.py. It NEVER calls anything that mutates state. The user reviews
recommendations and decides whether to act.

Why: surfacing > auto-tuning. With small sample sizes, the bot tuning
itself is the most reliable way to overfit and lose money.

Recommendations are produced when:
  - A bucket has at least MIN_TRADES_FOR_REC trades
  - Its shrunk win rate differs from overall by MIN_DEVIATION
  - AND its raw average PnL is consistent with the win rate signal
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from csp_screener.learning.feature_analyzer import BucketStats
from csp_screener.learning.ticker_scorer import TickerStats

MIN_TRADES_FOR_REC = 8
MIN_DEVIATION = 0.10        # 10 percentage points of win rate
TICKER_MIN_TRADES = 5
TICKER_MIN_DEVIATION = 0.15


@dataclass
class Recommendation:
    severity: str         # "info" | "warn" | "alert"
    category: str         # "ticker" | "feature_bucket" | "data_quality" | "summary"
    title: str
    detail: str
    supporting_data: dict


def recommend_from_tickers(ticker_stats: dict[str, TickerStats]) -> list[Recommendation]:
    """Surface tickers that are reliably winning or losing."""
    out = []
    for ticker, s in ticker_stats.items():
        if s.trades < TICKER_MIN_TRADES:
            continue
        delta = s.shrunk_win_rate - 0.5
        if delta < -TICKER_MIN_DEVIATION:
            out.append(Recommendation(
                severity="warn",
                category="ticker",
                title=f"{ticker} chronically losing",
                detail=(
                    f"{s.trades} closed trades, {s.wins} wins, "
                    f"shrunk win rate {s.shrunk_win_rate*100:.0f}% "
                    f"(avg ${s.avg_pnl:+.2f}). Consider removing from universe "
                    f"or temporarily excluding."
                ),
                supporting_data={
                    "trades": s.trades, "wins": s.wins,
                    "win_rate": s.shrunk_win_rate, "avg_pnl": s.avg_pnl,
                },
            ))
        elif delta > TICKER_MIN_DEVIATION:
            out.append(Recommendation(
                severity="info",
                category="ticker",
                title=f"{ticker} consistently winning",
                detail=(
                    f"{s.trades} closed trades, {s.wins} wins, "
                    f"shrunk win rate {s.shrunk_win_rate*100:.0f}% "
                    f"(avg ${s.avg_pnl:+.2f}). Ranker already boosts this; "
                    f"no change needed."
                ),
                supporting_data={
                    "trades": s.trades, "wins": s.wins,
                    "win_rate": s.shrunk_win_rate, "avg_pnl": s.avg_pnl,
                },
            ))
    # Sort warns first, then by deviation magnitude
    out.sort(key=lambda r: (r.severity != "warn",
                            -abs(r.supporting_data.get("win_rate", 0.5) - 0.5)))
    return out


def recommend_from_buckets(
    bucket_stats: list[BucketStats],
    overall_baseline: dict,
) -> list[Recommendation]:
    """Surface feature buckets that diverge significantly from baseline."""
    out = []
    baseline_wr = overall_baseline.get("win_rate", 0.5)

    for b in bucket_stats:
        if b.trades < MIN_TRADES_FOR_REC:
            continue
        if abs(b.deviation_from_overall) < MIN_DEVIATION:
            continue
        worse = b.deviation_from_overall < 0
        sign = "↓" if worse else "↑"
        severity = "warn" if worse else "info"
        out.append(Recommendation(
            severity=severity,
            category="feature_bucket",
            title=f"{b.feature} = {b.bucket}: {b.shrunk_win_rate*100:.0f}% win {sign}",
            detail=(
                f"{b.trades} trades in this bucket. Win rate "
                f"{b.shrunk_win_rate*100:.0f}% vs overall {baseline_wr*100:.0f}% "
                f"(deviation {b.deviation_from_overall*100:+.0f}pp). "
                f"Avg PnL ${b.avg_pnl:+.2f}, total ${b.total_pnl:+.2f}. "
                f"{'Consider narrowing this band.' if worse else 'Consider weighting toward this band.'}"
            ),
            supporting_data={
                "feature": b.feature, "bucket": b.bucket,
                "trades": b.trades, "win_rate": b.shrunk_win_rate,
                "deviation_pp": round(b.deviation_from_overall * 100, 1),
                "avg_pnl": b.avg_pnl,
            },
        ))
    out.sort(key=lambda r: (r.severity != "warn",
                            -abs(r.supporting_data.get("deviation_pp", 0))))
    return out


def overall_health(overall_baseline: dict, ticker_stats: dict[str, TickerStats]) -> Recommendation:
    """A one-line system health summary."""
    n = overall_baseline.get("trades", 0)
    wr = overall_baseline.get("win_rate", 0.0)
    avg = overall_baseline.get("avg_pnl", 0.0)
    if n == 0:
        return Recommendation(
            severity="info",
            category="summary",
            title="No closed trades yet",
            detail="Need at least 5 closed virtual trades for any learning signal. "
                   "Check back in 2-3 weeks.",
            supporting_data={"trades": n},
        )
    if n < 10:
        return Recommendation(
            severity="info",
            category="summary",
            title=f"Sample still thin ({n} trades)",
            detail=f"Win rate {wr*100:.0f}%, avg ${avg:+.2f}/trade. "
                   f"Treat all recommendations as tentative until you cross ~30 trades.",
            supporting_data={"trades": n, "win_rate": wr, "avg_pnl": avg},
        )
    severity = "warn" if avg < 0 else "info"
    return Recommendation(
        severity=severity,
        category="summary",
        title=f"{n} trades · win rate {wr*100:.0f}% · avg ${avg:+.2f}",
        detail=(
            f"{'Strategy is currently negative-EV. Do not deploy real money.' if avg < 0 else 'Strategy showing positive EV. Continue paper tracking before real deployment.'}"
        ),
        supporting_data={"trades": n, "win_rate": wr, "avg_pnl": avg},
    )


def all_recommendations(
    overall_baseline: dict,
    ticker_stats: dict[str, TickerStats],
    bucket_stats: list[BucketStats],
) -> list[Recommendation]:
    """Top-level: produce a ranked list of recommendations for the user."""
    out = [overall_health(overall_baseline, ticker_stats)]
    out.extend(recommend_from_tickers(ticker_stats))
    out.extend(recommend_from_buckets(bucket_stats, overall_baseline))
    return out
