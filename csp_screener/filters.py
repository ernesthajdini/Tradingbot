"""
Universe filters. Each filter takes ticker context, returns (passed, reason).

Filters are deliberately STRICT — better to skip a name than enter a bad trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from csp_screener import config


@dataclass
class TickerContext:
    """Per-ticker data needed by filters."""
    ticker: str
    last_price: float
    avg_volume_20d: float
    next_earnings: Optional[datetime] = None
    excluded: bool = False
    price_history: Optional[pd.Series] = None  # for ranker, not filters
    # Tier-specific price band; defaults = sandbox band. Live tier sets
    # LIVE_PRICE_MIN/MAX when building contexts.
    price_min: float = config.PRICE_MIN
    price_max: float = config.PRICE_MAX

    def __post_init__(self):
        # Normalize earnings to datetime if it's a date or string
        if self.next_earnings is not None and not isinstance(self.next_earnings, datetime):
            try:
                self.next_earnings = pd.Timestamp(self.next_earnings).to_pydatetime()
            except Exception:
                self.next_earnings = None


@dataclass
class FilterResult:
    passed: bool
    reason: str   # human-readable; empty string = passed


def filter_price(ctx: TickerContext) -> FilterResult:
    """Reject if price outside the context's tier band."""
    if ctx.last_price is None or pd.isna(ctx.last_price):
        return FilterResult(False, "no price data")
    if ctx.last_price < ctx.price_min:
        return FilterResult(False, f"price ${ctx.last_price:.2f} < ${ctx.price_min}")
    if ctx.last_price > ctx.price_max:
        return FilterResult(False, f"price ${ctx.last_price:.2f} > ${ctx.price_max}")
    return FilterResult(True, "")


def filter_volume(ctx: TickerContext) -> FilterResult:
    """Reject if 20-day avg volume below MIN_DAILY_VOLUME."""
    if ctx.avg_volume_20d is None or pd.isna(ctx.avg_volume_20d):
        return FilterResult(False, "no volume data")
    if ctx.avg_volume_20d < config.MIN_DAILY_VOLUME:
        return FilterResult(
            False,
            f"vol {ctx.avg_volume_20d:,.0f} < {config.MIN_DAILY_VOLUME:,}",
        )
    return FilterResult(True, "")


def filter_earnings(ctx: TickerContext, now: Optional[datetime] = None) -> FilterResult:
    """
    Reject if earnings within EARNINGS_EXCLUSION_DAYS.

    If we have NO earnings data (next_earnings is None), we PASS — but the caller
    is expected to surface this in the email as a "data quality" caveat.
    Missing data should never silently kill a good candidate, but a known
    upcoming earnings always wins.
    """
    if not config.NEVER_HOLD_THROUGH_EARNINGS:
        return FilterResult(True, "earnings rule disabled")
    if ctx.next_earnings is None:
        return FilterResult(True, "")  # no data, no rejection
    now = now or datetime.now()
    days_until = (ctx.next_earnings - now).days
    if 0 <= days_until <= config.EARNINGS_EXCLUSION_DAYS:
        return FilterResult(
            False,
            f"earnings in {days_until}d (need >{config.EARNINGS_EXCLUSION_DAYS}d)",
        )
    return FilterResult(True, "")


def filter_exclusion(ctx: TickerContext) -> FilterResult:
    """Reject if user has flagged this ticker for exclusion."""
    if ctx.excluded:
        return FilterResult(False, "user-excluded")
    return FilterResult(True, "")


# All filters in order. Order matters for performance (cheap rejections first).
ALL_FILTERS = [
    filter_exclusion,
    filter_price,
    filter_volume,
    filter_earnings,
]


def apply_all_filters(ctx: TickerContext, now: Optional[datetime] = None) -> FilterResult:
    """Apply all filters in order; first rejection wins."""
    for f in ALL_FILTERS:
        try:
            # filter_earnings takes optional now — others ignore
            if f is filter_earnings:
                result = f(ctx, now=now)
            else:
                result = f(ctx)
        except Exception as e:
            return FilterResult(False, f"filter {f.__name__} crashed: {e}")
        if not result.passed:
            return result
    return FilterResult(True, "")
