"""
For each ranked candidate, generate a concrete VIRTUAL setup:
  - which put strike (closest to target delta or % OTM)
  - which expiration (within DTE window)
  - estimated premium / max loss / breakeven
  - data quality flags

This is what gets tracked as a "virtual trade" by virtual_tracker, so the
screener accumulates real performance data even when no live order is placed.

We are deliberately permissive about MISSING data — if greeks or IV are
unavailable, we still generate a setup based on % OTM, but flag the data
quality so the email shows the caveat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

from csp_screener import config
from csp_screener.options_data import OptionsChain, OptionContract

logger = logging.getLogger(__name__)


@dataclass
class VirtualSetup:
    ticker: str
    spot_at_screen: float
    expiration: str          # ISO date
    dte: int
    strike: float
    pct_otm: float           # how far OTM as fraction of spot
    delta: Optional[float]
    iv: Optional[float]
    bid: float
    ask: float
    mid: float
    bid_ask_pct: float
    open_interest: int
    volume: int
    estimated_credit_per_contract: float  # premium received per contract (100 shares)
    max_loss_per_contract: float          # strike * 100 - credit (assignment risk)
    breakeven: float                      # strike - credit_per_share
    data_quality: str                     # "ibkr_greeks" / "yfinance_iv" / "estimated"
    reasoning: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _pick_best_put(
    chain: OptionsChain,
    expiration: datetime,
    spot: float,
    target_delta: float = config.TARGET_DELTA,
) -> tuple[Optional[OptionContract], list[str]]:
    """
    Pick the put closest to target_delta if delta is available, else closest
    to ~5% OTM as a proxy. Returns (contract, quality_warnings).

    Liquidity gates degrade gracefully instead of rejecting everything:

    - Live quotes (bid>0 and ask>0): enforce the spread gate strictly.
    - Stale quotes (bid=ask=0 but lastPrice>0): accept with a warning.
      This is the NORMAL state on weekends — the Sunday screen would
      otherwise never open a single virtual position.
    - OI: if ANY contract in this expiration reports OI, enforce MIN_OI.
      If the whole chain reports zero OI (IBKR snapshot without the OI
      generic tick, or a yfinance data gap), treat OI as UNKNOWN and
      accept with a warning rather than rejecting the entire chain.

    The warnings flow into the setup's data_quality/reasoning so the email
    and dashboard show exactly how much to trust the numbers.
    """
    candidates = chain.puts_for_expiration(expiration)
    if not candidates:
        return None, []

    quoted = [c for c in candidates if c.mid > 0]
    if not quoted:
        return None, []

    oi_known = any(c.open_interest > 0 for c in quoted)

    def passes(c: OptionContract) -> bool:
        has_live_quote = c.bid > 0 and c.ask > 0
        if has_live_quote:
            if c.bid_ask_spread_pct > config.MAX_BID_ASK_PCT_OF_MID:
                return False
        elif c.last <= 0:
            return False  # no live quote AND no last price — nothing to anchor on
        if oi_known and c.open_interest < config.MIN_OPEN_INTEREST:
            return False
        return True

    liquid = [c for c in quoted if passes(c)]
    if not liquid:
        return None, []

    with_delta = [c for c in liquid if c.delta is not None]
    if with_delta:
        chosen = min(with_delta, key=lambda c: abs(abs(c.delta) - target_delta))
    else:
        target_strike = spot * 0.95
        chosen = min(liquid, key=lambda c: abs(c.strike - target_strike))

    warnings: list[str] = []
    if not (chosen.bid > 0 and chosen.ask > 0):
        warnings.append(
            "STALE QUOTE: no live bid/ask (market closed?) — premium anchored "
            "to last trade. Verify in IBKR before any real order."
        )
    if not oi_known:
        warnings.append(
            "OI UNVERIFIED: no open-interest data in this chain snapshot. "
            "Check OI in IBKR/barchart before any real order."
        )
    return chosen, warnings


def generate_setup(
    ticker: str,
    spot: float,
    chain: Optional[OptionsChain],
    target_delta: float = config.TARGET_DELTA,
) -> Optional[VirtualSetup]:
    """
    Build a VirtualSetup for the given candidate. Returns None if no liquid
    contract is available in our DTE window.
    """
    reasoning: list[str] = []
    if chain is None:
        reasoning.append("no chain data — skipped")
        return None

    # Pick the expiration in the middle of our DTE window if possible
    if not chain.expirations:
        return None

    today = datetime.now().date()
    # Prefer mid-DTE expirations (35 ± 5)
    target_dte = (config.DTE_MIN + config.DTE_MAX) // 2
    expirations_sorted = sorted(
        chain.expirations,
        key=lambda e: abs((e.date() - today).days - target_dte),
    )

    chosen_contract = None
    chosen_exp = None
    quality_warnings: list[str] = []
    for exp in expirations_sorted:
        c, warns = _pick_best_put(chain, exp, spot, target_delta)
        if c is not None:
            chosen_contract = c
            chosen_exp = exp
            quality_warnings = warns
            break

    if chosen_contract is None:
        reasoning.append("no liquid put found in DTE window")
        return None

    credit_per_share = chosen_contract.mid
    credit_per_contract = credit_per_share * 100
    strike = chosen_contract.strike
    max_loss_per_contract = (strike * 100) - credit_per_contract
    breakeven = strike - credit_per_share

    # Determine data quality flag
    if chosen_contract.source == "ibkr" and chosen_contract.delta is not None:
        quality = "ibkr_greeks"
    elif chosen_contract.iv is not None:
        quality = "yfinance_iv_estimated_delta"
    else:
        quality = "premium_only_no_greeks"
    if quality_warnings:
        quality += "_unverified_liquidity"
        reasoning.extend(quality_warnings)

    reasoning.append(
        f"Selected put at strike ${strike:.2f}, expiration {chosen_exp.date()}, "
        f"DTE {chosen_contract.dte}, "
        f"{'delta ' + format(chosen_contract.delta, '+.3f') if chosen_contract.delta is not None else 'no delta'}"
    )
    reasoning.append(
        f"Liquidity OK: OI {chosen_contract.open_interest}, "
        f"bid/ask {chosen_contract.bid:.2f}/{chosen_contract.ask:.2f} "
        f"({chosen_contract.bid_ask_spread_pct:.1%} spread)"
    )

    return VirtualSetup(
        ticker=ticker,
        spot_at_screen=round(spot, 2),
        expiration=chosen_exp.date().isoformat(),
        dte=chosen_contract.dte,
        strike=strike,
        pct_otm=round((spot - strike) / spot, 4),
        delta=chosen_contract.delta,
        iv=chosen_contract.iv,
        bid=round(chosen_contract.bid, 4),
        ask=round(chosen_contract.ask, 4),
        mid=round(chosen_contract.mid, 4),
        bid_ask_pct=round(chosen_contract.bid_ask_spread_pct, 4),
        open_interest=chosen_contract.open_interest,
        volume=chosen_contract.volume,
        estimated_credit_per_contract=round(credit_per_contract, 2),
        max_loss_per_contract=round(max_loss_per_contract, 2),
        breakeven=round(breakeven, 2),
        data_quality=quality,
        reasoning=reasoning,
    )
