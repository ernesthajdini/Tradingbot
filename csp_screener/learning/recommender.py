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

import math
from dataclasses import dataclass
from typing import Optional

from csp_screener.learning.feature_analyzer import BucketStats
from csp_screener.learning.ticker_scorer import TickerStats

MIN_TRADES_FOR_REC = 8
MIN_DEVIATION = 0.10        # 10 percentage points of win rate
TICKER_MIN_TRADES = 5
TICKER_MIN_DEVIATION = 0.15


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    95% Wilson score interval for a binomial proportion.

    Why it exists: at n=8 the standard error of a win rate is ~17 points,
    so a 10-point deviation threshold alone emits noise wearing a badge —
    the exact 'statistical theater' the playbook killed the 50-trade gate
    for. A recommendation only fires when the interval EXCLUDES the
    baseline: fewer recommendations, but each one survives scrutiny.
    """
    if n == 0:
        return (0.0, 1.0)
    phat = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


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
        # Wilson gate: only speak when the 95% interval excludes coin-flip.
        # P&L-consistency gate: the rec text makes money claims ("chronically
        # losing" / "ranker already boosts this"), so the dollars must agree
        # with the win-rate signal — a high-win-rate ticker whose blow-ups
        # ate the profits gets NO "winning" rec (the scorer is in fact
        # penalizing it, and saying otherwise would be false).
        ci_lo, ci_hi = _wilson_interval(s.wins, s.trades)
        delta = s.shrunk_win_rate - 0.5
        if delta < -TICKER_MIN_DEVIATION and ci_hi < 0.5 and s.avg_pnl < 0:
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
                    "wilson_ci": [round(ci_lo, 3), round(ci_hi, 3)],
                },
            ))
        elif delta > TICKER_MIN_DEVIATION and ci_lo > 0.5 and s.avg_pnl > 0:
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
                    "wilson_ci": [round(ci_lo, 3), round(ci_hi, 3)],
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
        # Wilson gate vs the overall baseline win rate
        ci_lo, ci_hi = _wilson_interval(b.wins, b.trades)
        if ci_lo <= baseline_wr <= ci_hi:
            continue  # deviation not statistically distinguishable — noise
        worse = b.deviation_from_overall < 0
        # P&L-consistency gate (the ticker path has had one; this path did
        # not). Demonstrated failure: a bucket with 19 wins in 20 trades and
        # -$248 total P&L produced "86% win ↑ … consider weighting toward
        # this band". Never recommend weighting toward a money-loser, and
        # never warn away from a money-maker.
        if not worse and b.avg_pnl <= 0:
            continue
        if worse and b.avg_pnl > 0:
            continue
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
                "wilson_ci": [round(ci_lo, 3), round(ci_hi, 3)],
            },
        ))
    out.sort(key=lambda r: (r.severity != "warn",
                            -abs(r.supporting_data.get("deviation_pp", 0))))
    return out


def recommend_band_flips(closed_trades: list[dict]) -> list[Recommendation]:
    """
    Detect verdicts that FLIP between the base and pessimistic friction bands.

    Paper fills contain zero slippage information (playbook change #3), so a
    'winner' that is only a winner under base friction is exactly the trap the
    playbook was rewritten to avoid. Trades predating pnl_pessimistic fall
    back to base pnl (neutral — they can't create a flip on their own).
    """
    out: list[Recommendation] = []
    with_band = [t for t in closed_trades if t.get("pnl_pessimistic") is not None]
    if len(with_band) < TICKER_MIN_TRADES:
        return out

    def _sums(trades: list[dict]) -> tuple[float, float]:
        base = sum(float(t.get("pnl") or 0) for t in trades)
        pess = sum(
            float(t.get("pnl_pessimistic")
                  if t.get("pnl_pessimistic") is not None
                  else (t.get("pnl") or 0))
            for t in trades
        )
        return base, pess

    # Overall flip — the alarming one
    base_total, pess_total = _sums(closed_trades)
    if base_total > 0 and pess_total <= 0:
        out.append(Recommendation(
            severity="alert",
            category="friction_band",
            title="Track record is positive ONLY under optimistic fills",
            detail=(
                f"Base-friction P&L ${base_total:+.2f} flips to "
                f"${pess_total:+.2f} under the pessimistic slippage band. "
                f"Until live fills prove otherwise, treat the strategy as "
                f"unproven — do not deploy real money on this record."
            ),
            supporting_data={
                "pnl_base": round(base_total, 2),
                "pnl_pessimistic": round(pess_total, 2),
                "trades_with_band": len(with_band),
            },
        ))

    # Per-ticker flips
    by_ticker: dict[str, list[dict]] = {}
    for t in closed_trades:
        tk = t.get("ticker")
        if tk:
            by_ticker.setdefault(tk, []).append(t)
    for tk, trades in sorted(by_ticker.items()):
        # Gate on trades that actually HAVE band data — legacy trades fall
        # back to base pnl and cannot create a flip, so counting them as
        # evidence would let a single banded trade masquerade as N trades.
        banded = [t for t in trades if t.get("pnl_pessimistic") is not None]
        if len(banded) < TICKER_MIN_TRADES:
            continue
        base, pess = _sums(trades)
        if base > 0 and pess <= 0:
            out.append(Recommendation(
                severity="warn",
                category="friction_band",
                title=f"{tk} wins evaporate under pessimistic friction",
                detail=(
                    f"{len(banded)} banded trades ({len(trades)} total): "
                    f"${base:+.2f} at base friction, "
                    f"${pess:+.2f} at the pessimistic band. Its wins are "
                    f"thinner than the slippage uncertainty — don't trust "
                    f"this name until live fills narrow the band."
                ),
                supporting_data={
                    "ticker": tk, "trades": len(trades),
                    "banded_trades": len(banded),
                    "pnl_base": round(base, 2),
                    "pnl_pessimistic": round(pess, 2),
                },
            ))
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
    closed_trades: Optional[list[dict]] = None,
) -> list[Recommendation]:
    """Top-level: produce a ranked list of recommendations for the user."""
    out = [overall_health(overall_baseline, ticker_stats)]
    if closed_trades:
        out.extend(recommend_band_flips(closed_trades))
    out.extend(recommend_from_tickers(ticker_stats))
    out.extend(recommend_from_buckets(bucket_stats, overall_baseline))
    return out
