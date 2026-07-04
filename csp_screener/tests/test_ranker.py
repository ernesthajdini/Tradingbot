"""Tests for ranker.py — realized vol percentile."""
import numpy as np
import pandas as pd

from csp_screener.ranker import (
    annualized_realized_vol, compute_rv_percentile, rank_candidates,
)


def _make_price_series(n_days: int, drift: float = 0.0, vol: float = 0.01, seed: int = 42):
    np.random.seed(seed)
    daily_returns = np.random.normal(drift / 252, vol, n_days)
    prices = 100 * np.cumprod(1 + daily_returns)
    return pd.Series(prices, index=pd.date_range("2024-01-01", periods=n_days, freq="B"))


def test_realized_vol_increases_with_vol():
    low = _make_price_series(300, vol=0.005)
    high = _make_price_series(300, vol=0.02, seed=43)
    low_rv = annualized_realized_vol(np.log(low / low.shift(1)).dropna(), 20).iloc[-1]
    high_rv = annualized_realized_vol(np.log(high / high.shift(1)).dropna(), 20).iloc[-1]
    assert high_rv > low_rv


def test_compute_rv_percentile_returns_nan_for_short_history():
    s = _make_price_series(10)
    rv, pct = compute_rv_percentile(s)
    assert np.isnan(rv) and np.isnan(pct)


def test_compute_rv_percentile_in_range():
    s = _make_price_series(300)
    rv, pct = compute_rv_percentile(s)
    assert 0 <= pct <= 100
    assert rv > 0


def test_rank_candidates_orders_by_percentile():
    cands = [
        {"ticker": "LOW", "last_price": 10, "price_history": _make_price_series(300, vol=0.005, seed=1)},
        {"ticker": "MED", "last_price": 10, "price_history": _make_price_series(300, vol=0.01, seed=2)},
        {"ticker": "HIGH", "last_price": 10, "price_history": _make_price_series(300, vol=0.02, seed=3)},
    ]
    ranked = rank_candidates(cands, top_n=3)
    assert len(ranked) == 3
    # Percentiles should be sorted descending
    assert ranked[0].rv_percentile >= ranked[1].rv_percentile >= ranked[2].rv_percentile


def test_rank_candidates_skips_missing_data():
    cands = [
        {"ticker": "GOOD", "last_price": 10, "price_history": _make_price_series(300)},
        {"ticker": "BAD", "last_price": 10, "price_history": _make_price_series(5)},  # too short
    ]
    ranked = rank_candidates(cands, top_n=5)
    tickers = [r.ticker for r in ranked]
    assert "GOOD" in tickers
    assert "BAD" not in tickers
