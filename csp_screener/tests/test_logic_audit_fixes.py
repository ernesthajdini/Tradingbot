"""
Regression tests for the 2026-07-30 logic audit (48-agent, math-level).

Each test pins a bug that was measured against live market data:
  1. every spot price was >= 1 session stale (yfinance `end` is EXCLUSIVE)
  2. the live ticket printed a net credit ~2.3x what its own exit plan nets
  3. the earnings blackout was shorter than the actual holding period
  4. the real-money tier had NO open-interest floor (the sandbox had one)
  7. the scoreboard pooled two incompatible payoff distributions
  8. the bucket recommender could recommend a money-losing bucket
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from csp_screener import config, data_pipeline, evaluator, journal
from csp_screener.learning import feature_analyzer, recommender
from csp_screener.options_data import OptionContract, OptionsChain
from csp_screener.setup_generator import (
    earnings_inside_hold, generate_setup, generate_spread_setup, net_at_tp_exit,
)


@pytest.fixture(autouse=True)
def clean_journal(tmp_path, monkeypatch):
    new_files = {topic: tmp_path / f"{topic}.jsonl" for topic in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", new_files)
    yield


EXP = datetime.now() + timedelta(days=35)
FRESH = datetime.now() - timedelta(days=1)


# ---------------------------------------------------------------------------
# BUG 1 — stale prices
# ---------------------------------------------------------------------------

def _frame(last_date, days=40):
    idx = pd.date_range(end=last_date, periods=days, freq="D")
    return pd.DataFrame(
        {"Close": [10.0] * days, "Adj Close": [10.0] * days, "Volume": [2_000_000] * days},
        index=idx,
    )


def test_is_stale_flags_old_bars_but_tolerates_weekends():
    assert data_pipeline.is_stale(_frame(datetime.now() - timedelta(days=9)))
    assert not data_pipeline.is_stale(_frame(datetime.now()))
    assert not data_pipeline.is_stale(_frame(datetime.now() - timedelta(days=3)))  # long weekend
    assert data_pipeline.is_stale(None)
    assert data_pipeline.is_stale(pd.DataFrame())


def test_statistics_exclude_todays_partial_bar():
    # Today's bar carries partial volume; including it understates the average.
    df = _frame(datetime.now())
    df.iloc[-1, df.columns.get_loc("Volume")] = 1  # a few minutes of trading
    # 20-day average must ignore that partial bar entirely
    assert data_pipeline.avg_volume_20d(df) == pytest.approx(2_000_000)


# ---------------------------------------------------------------------------
# BUG 2 — the ticket's net credit vs what its exit plan delivers
# ---------------------------------------------------------------------------

def test_net_at_tp_exit_is_far_below_hold_to_expiry():
    for gross in (40.0, 50.0, 70.0, 100.0):
        hold_to_expiry = gross - (4 * config.COMMISSION_PER_CONTRACT
                                  + 2 * config.SLIPPAGE_PCT_OF_PREMIUM * gross)
        at_tp = net_at_tp_exit(gross, "put_credit_spread")
        assert at_tp < hold_to_expiry
        assert hold_to_expiry / at_tp > 2.0  # the ~2.3x overstatement


def test_net_at_tp_exit_matches_the_sandbox_gate_floor():
    # The gate refuses credits that cannot net > 0 at their own take-profit
    assert net_at_tp_exit(4.0, "csp") < 0
    assert net_at_tp_exit(10.0, "csp") > 0


# ---------------------------------------------------------------------------
# BUG 3 — earnings inside the holding period
# ---------------------------------------------------------------------------

def test_earnings_inside_hold_covers_the_real_calendar_case():
    # The measured production case: a 37-DTE expiry holds 16 days, but the
    # ticker filter only blacks out 15 — earnings at 16 days slipped through.
    assert earnings_inside_hold(dte=37, next_earnings_days=16)
    assert earnings_inside_hold(dte=37, next_earnings_days=10)
    assert not earnings_inside_hold(dte=37, next_earnings_days=17)
    # Unknown earnings is the ticker filter's call, not this gate's
    assert not earnings_inside_hold(dte=37, next_earnings_days=None)


def _put(strike, mid, delta, oi=2000):
    half = max(0.005, mid * 0.01)
    return OptionContract(
        ticker="X", expiration=EXP, strike=strike, right="P",
        bid=round(mid - half, 4), ask=round(mid + half, 4), last=mid, mid=mid,
        open_interest=oi, volume=200, iv=0.5, delta=delta,
        source="yfinance", last_trade_date=FRESH,
    )


def test_generator_skips_expiry_whose_hold_spans_earnings():
    chain = OptionsChain(ticker="X", spot=20.0, expirations=[EXP],
                         puts=[_put(18.0, 0.30, -0.25)], source="yfinance")
    dte = (EXP.date() - datetime.now().date()).days
    hold = max(0, dte - config.VIRTUAL_FORCE_EXIT_DTE)
    assert generate_setup("X", 20.0, chain, next_earnings_days=hold) is None
    assert generate_setup("X", 20.0, chain, next_earnings_days=hold + 5) is not None


# ---------------------------------------------------------------------------
# BUG 4 — the live tier's missing open-interest floor
# ---------------------------------------------------------------------------

def test_live_spread_requires_open_interest():
    thin = OptionsChain(
        ticker="X", spot=26.0, expirations=[EXP], source="yfinance",
        puts=[_put(24.0, 0.95, -0.28, oi=30), _put(22.0, 0.20, -0.12, oi=17)])
    diags: list = []
    assert generate_spread_setup("X", 26.0, thin, diagnostics=diags) is None
    assert any("OI" in d for d in diags)

    liquid = OptionsChain(
        ticker="X", spot=26.0, expirations=[EXP], source="yfinance",
        puts=[_put(24.0, 0.95, -0.28, oi=4000), _put(22.0, 0.20, -0.12, oi=3000)])
    assert generate_spread_setup("X", 26.0, liquid) is not None


def test_live_spread_degrades_when_no_oi_data_at_all():
    # A snapshot with zero OI everywhere is UNKNOWN, not "all illiquid"
    chain = OptionsChain(
        ticker="X", spot=26.0, expirations=[EXP], source="yfinance",
        puts=[_put(24.0, 0.95, -0.28, oi=0), _put(22.0, 0.20, -0.12, oi=0)])
    assert generate_spread_setup("X", 26.0, chain) is not None


# ---------------------------------------------------------------------------
# BUG 7 — tier-separated scoreboards
# ---------------------------------------------------------------------------

def _closed(ticker, tier, pnl):
    tid = f"s::{ticker}::10.0::2026-09-18"
    journal.append("virtual_trades", {
        "event": "open", "trade_id": tid, "ticker": ticker, "strike": 10.0,
        "expiration": "2026-09-18", "opened_at": datetime.now().isoformat(),
        "spot_at_open": 14.0, "dte_at_open": 35, "credit_received": 40.0,
        "max_loss": 960.0, "breakeven": 9.6, "delta_at_open": -0.2, "tier": tier,
    })
    journal.append("virtual_trades", {
        "event": "close", "trade_id": tid, "ticker": ticker, "strike": 10.0,
        "expiration": "2026-09-18", "closed_at": datetime.now().isoformat(),
        "exit_reason": "take_profit_50pct", "pnl": pnl, "tier": tier,
    })


def test_summaries_split_by_tier():
    _closed("AAA", "live", 20.0)
    _closed("BBB", "sandbox", -50.0)
    pooled = evaluator.compute_summary()
    live = evaluator.compute_summary(tier="live")
    sandbox = evaluator.compute_summary(tier="sandbox")
    assert pooled.closed_count == 2
    assert live.closed_count == 1 and live.total_pnl == pytest.approx(20.0)
    assert sandbox.closed_count == 1 and sandbox.total_pnl == pytest.approx(-50.0)
    assert set(evaluator.all_periods_summary()) >= {"live", "sandbox", "all"}


# ---------------------------------------------------------------------------
# BUG 8 — never recommend weighting toward a money-losing bucket
# ---------------------------------------------------------------------------

def test_bucket_recommendation_requires_pnl_agreement():
    # 19 wins in 20 trades but deeply negative P&L — the exact demonstrated
    # failure: "86% win ↑ … consider weighting toward this band".
    trades = ([{"ticker": "A", "pnl": 2.0, "delta_at_open": -0.30} for _ in range(19)]
              + [{"ticker": "A", "pnl": -286.0, "delta_at_open": -0.30}])
    buckets = feature_analyzer.analyze_features(trades)
    recs = recommender.recommend_from_buckets(buckets, {"win_rate": 0.5})
    assert all("weighting toward" not in r.detail for r in recs)
