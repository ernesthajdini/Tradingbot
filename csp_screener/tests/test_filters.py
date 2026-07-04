"""Tests for filters.py — strict rejection logic."""
from datetime import datetime, timedelta

import pytest

from csp_screener import config
from csp_screener.filters import (
    TickerContext, apply_all_filters,
    filter_price, filter_volume, filter_earnings, filter_exclusion,
)


def _ctx(**overrides):
    base = dict(
        ticker="TEST",
        last_price=15.0,
        avg_volume_20d=5_000_000.0,
        next_earnings=None,
        excluded=False,
    )
    base.update(overrides)
    return TickerContext(**base)


def test_price_pass():
    assert filter_price(_ctx(last_price=12.0)).passed


def test_price_too_low():
    r = filter_price(_ctx(last_price=2.0))
    assert not r.passed
    assert "price" in r.reason


def test_price_too_high():
    r = filter_price(_ctx(last_price=100.0))
    assert not r.passed
    assert "price" in r.reason


def test_price_nan():
    r = filter_price(_ctx(last_price=float("nan")))
    assert not r.passed


def test_volume_pass():
    assert filter_volume(_ctx(avg_volume_20d=2_000_000)).passed


def test_volume_fail():
    r = filter_volume(_ctx(avg_volume_20d=100_000))
    assert not r.passed
    assert "vol" in r.reason


def test_earnings_no_data_passes():
    assert filter_earnings(_ctx(next_earnings=None)).passed


def test_earnings_within_window_fails():
    now = datetime(2026, 5, 19)
    soon = now + timedelta(days=3)
    r = filter_earnings(_ctx(next_earnings=soon), now=now)
    assert not r.passed
    assert "earnings" in r.reason


def test_earnings_outside_window_passes():
    now = datetime(2026, 5, 19)
    far = now + timedelta(days=30)
    assert filter_earnings(_ctx(next_earnings=far), now=now).passed


def test_earnings_already_past_passes():
    now = datetime(2026, 5, 19)
    past = now - timedelta(days=5)
    assert filter_earnings(_ctx(next_earnings=past), now=now).passed


def test_exclusion():
    assert not filter_exclusion(_ctx(excluded=True)).passed
    assert filter_exclusion(_ctx(excluded=False)).passed


def test_apply_all_short_circuits():
    """First rejection should win and downstream filters not crash."""
    ctx = _ctx(excluded=True, last_price=float("nan"))  # exclusion runs first
    r = apply_all_filters(ctx)
    assert not r.passed
    assert "excluded" in r.reason
