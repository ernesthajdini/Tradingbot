"""
Quote/credit sanity gates.

WHY THIS EXISTS (production incident, Jul 2026): yfinance returned a
self-contradicting chain row for LCID — delta -0.12 (deep OTM) with a
$1.75/share premium (deep ITM pricing) on a $3.50 strike. The screener took
the quote at face value, booked a $175.50 credit on a put actually worth a
few cents, and the resulting position churned fake +$77 "take-profit wins"
into the track record daily. A 100% win rate built on one broken quote.

The rules here are deliberately crude and generous — they only need to
catch order-of-magnitude garbage, not price options:

  - A short-dated OTM put's premium is a small fraction of its strike.
    Even a 150%-IV meme stock at 30-delta / 45 DTE prices under ~10% of
    strike. We cap at 12%.
  - If the row itself claims deep OTM (|delta| < 0.15), the bound tightens
    to 5% of strike — a self-contradicting row (tiny delta, fat premium)
    is the exact LCID signature.
  - A put credit spread's net credit can never exceed the width between
    strikes; above 90% of width is garbage.

The same predicate is applied at three layers:
  1. setup_generator — garbage quotes never become setups (prevention)
  2. virtual_tracker / evaluator read paths — historical garbage trades
     are excluded from open positions, stats, and the learning layer
     (retroactive quarantine; the append-only journal is never mutated)
  3. dashboard-web/lib/queries.ts mirrors the rule for the Supabase reads

NOTE: constants live here, not in config.py — config.py is under the
14-day anti-tinker cooldown, and these are structural data-integrity
bounds, not tunable strategy thresholds.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Max credible credit as a fraction of strike value for a short OTM put
MAX_CSP_CREDIT_FRAC_OF_STRIKE = 0.12
# Tighter bound when the row itself says deep OTM
DEEP_OTM_DELTA = 0.15
MAX_DEEP_OTM_CREDIT_FRAC = 0.05
# A spread's net credit cannot approach the strike width
MAX_SPREAD_CREDIT_FRAC_OF_WIDTH = 0.90


def csp_credit_is_sane(
    credit_per_contract: float,
    strike: float,
    delta: Optional[float] = None,
) -> tuple[bool, Optional[str]]:
    """(ok, reason_if_not) for a cash-secured put's quoted credit."""
    if strike <= 0:
        return False, f"non-positive strike {strike}"
    if credit_per_contract <= 0:
        return False, f"non-positive credit ${credit_per_contract:.2f}"
    frac = credit_per_contract / (strike * 100.0)
    if frac > MAX_CSP_CREDIT_FRAC_OF_STRIKE:
        return False, (
            f"credit ${credit_per_contract:.2f} is {frac:.0%} of strike value — "
            f"implausible for an OTM put (cap {MAX_CSP_CREDIT_FRAC_OF_STRIKE:.0%}); "
            f"quote is garbage/stale"
        )
    if delta is not None and abs(delta) < DEEP_OTM_DELTA and frac > MAX_DEEP_OTM_CREDIT_FRAC:
        return False, (
            f"self-contradicting row: delta {delta:+.2f} (deep OTM) but credit "
            f"is {frac:.0%} of strike (cap {MAX_DEEP_OTM_CREDIT_FRAC:.0%} at this delta)"
        )
    return True, None


def spread_credit_is_sane(
    credit_per_contract: float,
    short_strike: float,
    long_strike: float,
) -> tuple[bool, Optional[str]]:
    """(ok, reason_if_not) for a put credit spread's net credit."""
    width = (short_strike - long_strike) * 100.0
    if width <= 0:
        return False, f"non-positive spread width ({short_strike}/{long_strike})"
    if credit_per_contract <= 0:
        return False, f"non-positive credit ${credit_per_contract:.2f}"
    if credit_per_contract > width * MAX_SPREAD_CREDIT_FRAC_OF_WIDTH:
        return False, (
            f"net credit ${credit_per_contract:.2f} exceeds "
            f"{MAX_SPREAD_CREDIT_FRAC_OF_WIDTH:.0%} of the ${width:.0f} strike "
            f"width — implausible; quote is garbage/stale"
        )
    return True, None


def open_event_is_sane(ev: dict) -> bool:
    """
    Retroactive gate for journal open events (read paths). Trades that fail
    are excluded from open-position replay, performance stats, and learning —
    the journal rows themselves are never touched.
    """
    try:
        credit = float(ev.get("credit_received") or 0)
        strike = float(ev.get("strike") or 0)
    except (ValueError, TypeError):
        return False
    structure = ev.get("structure") or "csp"
    if structure == "put_credit_spread" and ev.get("long_strike") is not None:
        try:
            ok, _ = spread_credit_is_sane(credit, strike, float(ev["long_strike"]))
            return ok
        except (ValueError, TypeError):
            return False
    delta = ev.get("delta_at_open")
    try:
        delta = float(delta) if delta is not None else None
    except (ValueError, TypeError):
        delta = None
    ok, _ = csp_credit_is_sane(credit, strike, delta)
    return ok
