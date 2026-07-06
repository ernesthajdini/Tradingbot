"""
FOMC calendar awareness (playbook change #13 — paper-only module).

Decision days for 2026-2027 per the Federal Reserve's published schedule.
The screener flags FOMC weeks in the email; the paper condor module enters
T-1 during the normal evening window and exits next morning via GTC.
Live capital: never this year — 8 events x ~$60 credit defending $130 max
loss is a hobby line one 2022-style surprise erases.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

# Second day of each two-day meeting = decision day (14:00 ET statement).
FOMC_DECISION_DATES: list[date] = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 9),
    # 2027 (projected typical cadence; refresh when the Fed publishes)
    date(2027, 1, 27), date(2027, 3, 17), date(2027, 4, 28),
    date(2027, 6, 16), date(2027, 7, 28), date(2027, 9, 15),
    date(2027, 10, 27), date(2027, 12, 8),
]


def next_fomc(today: Optional[date] = None) -> Optional[date]:
    today = today or date.today()
    future = [d for d in FOMC_DECISION_DATES if d >= today]
    return min(future) if future else None


def days_to_fomc(today: Optional[date] = None) -> Optional[int]:
    today = today or date.today()
    nxt = next_fomc(today)
    return (nxt - today).days if nxt else None


def is_fomc_week(today: Optional[date] = None) -> bool:
    d = days_to_fomc(today)
    return d is not None and d <= 5
