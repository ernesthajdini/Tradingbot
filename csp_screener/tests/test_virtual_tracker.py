"""
Tests for virtual_tracker.py — the self-evaluation engine.

We focus on:
  - Black-Scholes put price sanity
  - State reconstruction from event log
  - Exit rule firing logic
"""
from datetime import datetime, timedelta

import pytest

from csp_screener import config, journal, virtual_tracker
from csp_screener.setup_generator import VirtualSetup


@pytest.fixture(autouse=True)
def clean_journal(tmp_path, monkeypatch):
    new_files = {topic: tmp_path / f"{topic}.jsonl" for topic in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", new_files)
    yield


def _setup(**overrides):
    base = dict(
        ticker="TEST",
        spot_at_screen=20.0,
        expiration=(datetime.now() + timedelta(days=35)).date().isoformat(),
        dte=35,
        strike=19.0,
        pct_otm=0.05,
        delta=-0.30,
        iv=0.50,
        bid=0.45,
        ask=0.55,
        mid=0.50,
        bid_ask_pct=0.04,
        open_interest=1000,
        volume=500,
        estimated_credit_per_contract=50.0,
        max_loss_per_contract=1850.0,
        breakeven=18.50,
        data_quality="ibkr_greeks",
        reasoning=["test setup"],
    )
    base.update(overrides)
    return VirtualSetup(**base)


# ---- BS pricing ----

def test_bs_put_at_expiry_is_intrinsic():
    # Spot $20, strike $19 -> put worth 0 at expiry
    assert virtual_tracker.black_scholes_put_price(20.0, 19.0, 0, 0.5) == 0.0
    # Spot $18, strike $19 -> put worth $1 at expiry
    assert virtual_tracker.black_scholes_put_price(18.0, 19.0, 0, 0.5) == pytest.approx(1.0)


def test_bs_put_higher_vol_higher_price():
    low = virtual_tracker.black_scholes_put_price(20.0, 19.0, 30, 0.20)
    high = virtual_tracker.black_scholes_put_price(20.0, 19.0, 30, 0.60)
    assert high > low


def test_bs_put_lower_dte_lower_price():
    short = virtual_tracker.black_scholes_put_price(20.0, 19.0, 5, 0.40)
    long_ = virtual_tracker.black_scholes_put_price(20.0, 19.0, 50, 0.40)
    assert long_ > short


# ---- State reconstruction ----

def test_open_and_close_lifecycle():
    s = _setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_test")

    opens = virtual_tracker.get_open_virtual_trades()
    assert len(opens) == 1
    assert opens[0].trade_id == tid

    virtual_tracker.close_virtual_position(
        trade_id=tid, exit_reason="test", exit_spot=21.0,
        final_put_price=20.0, credit_received=50.0,
    )

    opens = virtual_tracker.get_open_virtual_trades()
    assert len(opens) == 0


def test_multiple_opens_no_collision():
    s1 = _setup(ticker="A", strike=10)
    s2 = _setup(ticker="B", strike=20)
    virtual_tracker.open_virtual_position(s1, "screen_X")
    virtual_tracker.open_virtual_position(s2, "screen_X")
    assert len(virtual_tracker.get_open_virtual_trades()) == 2


# ---- Exit logic ----

def test_exit_at_50pct_profit():
    s = _setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_test")
    trade = virtual_tracker.get_open_virtual_trades()[0]

    # If the put is now worth $25 (50% of $50 credit absorbed), TP should fire
    # spot moves up means put price falls. We'll force this via direct call.
    result = virtual_tracker.evaluate_open_position(
        trade, current_spot=22.0, current_iv=0.20,
    )
    # With spot $22 strike $19 IV 20% and ~35 DTE, put is well OTM and cheap
    assert result["exit_now"]
    assert "take_profit" in result["exit_reason"] or "force_exit" in result["exit_reason"]


def test_exit_at_21_dte():
    s = _setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_test")
    trade = virtual_tracker.get_open_virtual_trades()[0]
    # Fake a 'today' that's 15 days past open (so 35-15=20 DTE remaining)
    fake_today = datetime.now() + timedelta(days=15)
    result = virtual_tracker.evaluate_open_position(
        trade, current_spot=20.0, current_iv=0.50, today=fake_today,
    )
    assert result["exit_now"]
    # Could be force_exit_dte OR take_profit if BS makes it profitable
    assert result["exit_reason"] in ("force_exit_dte", "take_profit_50pct")


def test_no_exit_when_neutral():
    s = _setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_test")
    trade = virtual_tracker.get_open_virtual_trades()[0]
    # Spot unchanged, full DTE -> no exit yet
    result = virtual_tracker.evaluate_open_position(
        trade, current_spot=20.0, current_iv=0.50,
    )
    assert not result["exit_now"]
    assert result["exit_reason"] is None


def test_stop_loss_fires_on_big_loss():
    s = _setup(estimated_credit_per_contract=50.0)
    tid = virtual_tracker.open_virtual_position(s, "screen_test")
    trade = virtual_tracker.get_open_virtual_trades()[0]
    # Spot crashes to $14, put is deep ITM
    result = virtual_tracker.evaluate_open_position(
        trade, current_spot=14.0, current_iv=0.80,
    )
    assert result["exit_now"]
    assert result["exit_reason"] == "stop_loss_2x_credit"
