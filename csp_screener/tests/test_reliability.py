"""
Tests for the reliability-hardening pass:
  - close events carry ticker/strike/expiration (Supabase NOT NULL)
  - contract-level dedup of virtual opens + MAX_VIRTUAL_OPEN cap
  - tz-aware timestamps don't crash the evaluator
  - NaN-safe option chain parsing
  - liquidity gates degrade gracefully (stale quotes / unknown OI)
"""
from datetime import datetime, timedelta

import math
import pytest

from csp_screener import config, evaluator, journal, virtual_tracker
from csp_screener.options_data import OptionContract, OptionsChain, _num
from csp_screener.setup_generator import VirtualSetup, _pick_best_put


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
        bid=0.45, ask=0.55, mid=0.50, bid_ask_pct=0.04,
        open_interest=1000, volume=500,
        estimated_credit_per_contract=50.0,
        max_loss_per_contract=1850.0,
        breakeven=18.50,
        data_quality="ibkr_greeks",
        reasoning=["test"],
    )
    base.update(overrides)
    return VirtualSetup(**base)


# ---------------------------------------------------------------------------
# Close events must carry ticker/strike/expiration
# ---------------------------------------------------------------------------

def test_close_event_includes_ticker_when_passed():
    s = _setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_x")
    rec = virtual_tracker.close_virtual_position(
        trade_id=tid, exit_reason="test", exit_spot=21.0,
        final_put_price=10.0, credit_received=50.0,
        ticker="TEST", strike=19.0, expiration=s.expiration,
    )
    assert rec["ticker"] == "TEST"
    assert rec["strike"] == 19.0
    assert rec["expiration"] == s.expiration


def test_close_event_parses_ticker_from_trade_id_fallback():
    s = _setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_y")
    # Old-style call without ticker — must recover from trade_id
    rec = virtual_tracker.close_virtual_position(
        trade_id=tid, exit_reason="test", exit_spot=21.0,
        final_put_price=10.0, credit_received=50.0,
    )
    assert rec["ticker"] == "TEST"
    assert rec["strike"] == 19.0
    assert rec["expiration"] == s.expiration


def test_update_all_closes_carry_ticker():
    s = _setup()
    virtual_tracker.open_virtual_position(s, "screen_z")

    summary = virtual_tracker.update_all_open_positions(
        spot_resolver=lambda t: 25.0,   # far above strike → TP fires
        iv_resolver=lambda t: 0.20,
    )
    assert summary["closed"] == 1
    closes = journal.read_filtered("virtual_trades", event="close")
    assert len(closes) == 1
    assert closes[0]["ticker"] == "TEST"
    assert closes[0]["strike"] == 19.0


# ---------------------------------------------------------------------------
# Dedup + cap in step_open_virtual_positions
# ---------------------------------------------------------------------------

def test_step_open_dedups_same_contract():
    from csp_screener.main import step_open_virtual_positions
    s = _setup()
    candidate = {"ticker": "TEST", "setup": s.to_dict()}

    first = step_open_virtual_positions([candidate], "screen_a")
    assert len(first) == 1
    # Re-run (different screen_id — simulates a Sunday re-run): must skip
    second = step_open_virtual_positions([candidate], "screen_b")
    assert len(second) == 0
    assert len(virtual_tracker.get_open_virtual_trades()) == 1


def test_step_open_respects_max_virtual_open(monkeypatch):
    from csp_screener.main import step_open_virtual_positions
    monkeypatch.setattr(config, "MAX_VIRTUAL_OPEN", 2)

    candidates = []
    for i, tk in enumerate(["AAA", "BBB", "CCC"]):
        s = _setup(ticker=tk, strike=10.0 + i)
        candidates.append({"ticker": tk, "setup": s.to_dict()})

    opened = step_open_virtual_positions(candidates, "screen_cap")
    assert len(opened) == 2
    assert len(virtual_tracker.get_open_virtual_trades()) == 2


# ---------------------------------------------------------------------------
# Evaluator handles tz-aware closed_at (Supabase-hydrated records)
# ---------------------------------------------------------------------------

def test_evaluator_handles_tz_aware_closed_at():
    # Write an open + tz-aware close directly to the journal
    journal.append("virtual_trades", {
        "event": "open", "trade_id": "t1", "ticker": "A", "strike": 10.0,
        "expiration": "2026-08-01", "opened_at": datetime.now().isoformat(),
        "spot_at_open": 11.0, "dte_at_open": 30, "credit_received": 40.0,
        "max_loss": 960.0, "breakeven": 9.6,
    })
    journal.append("virtual_trades", {
        "event": "close", "trade_id": "t1", "ticker": "A",
        "closed_at": datetime.now().isoformat() + "+00:00",  # tz-aware!
        "exit_reason": "take_profit_50pct", "pnl": 20.0,
        "pnl_pct_of_credit": 0.5, "credit_received": 40.0,
        "final_put_price": 20.0, "exit_spot": 11.5,
    })
    summary = evaluator.compute_summary(30, "test")
    assert summary.closed_count == 1
    assert summary.total_pnl == 20.0


# ---------------------------------------------------------------------------
# NaN-safe option parsing
# ---------------------------------------------------------------------------

def test_num_handles_nan_none_inf():
    assert _num(float("nan")) == 0.0
    assert _num(None) == 0.0
    assert _num(float("inf")) == 0.0
    assert _num("3.5") == 3.5
    assert _num(2) == 2.0
    assert _num("garbage") == 0.0


# ---------------------------------------------------------------------------
# Graceful liquidity gates
# ---------------------------------------------------------------------------

def _contract(strike, bid, ask, last, oi, exp, delta=None):
    mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
    return OptionContract(
        ticker="T", expiration=exp, strike=strike, right="P",
        bid=bid, ask=ask, last=last, mid=mid,
        open_interest=oi, volume=0, iv=0.4, delta=delta, source="yfinance",
    )


def test_pick_best_put_accepts_stale_quotes_with_warning():
    exp = datetime.now() + timedelta(days=35)
    # Weekend state: bid=ask=0, lastPrice present, OI present
    chain = OptionsChain(ticker="T", spot=20.0, expirations=[exp])
    chain.puts = [_contract(19.0, 0, 0, 0.50, 800, exp)]
    chosen, warns = _pick_best_put(chain, exp, 20.0)
    assert chosen is not None
    assert any("STALE QUOTE" in w for w in warns)


def test_pick_best_put_accepts_unknown_oi_chain_with_warning():
    exp = datetime.now() + timedelta(days=35)
    # Whole chain reports OI=0 (data gap) but live quotes exist
    chain = OptionsChain(ticker="T", spot=20.0, expirations=[exp])
    chain.puts = [_contract(19.0, 0.45, 0.47, 0.46, 0, exp)]
    chosen, warns = _pick_best_put(chain, exp, 20.0)
    assert chosen is not None
    assert any("OI UNVERIFIED" in w for w in warns)


def test_pick_best_put_still_enforces_oi_when_known():
    exp = datetime.now() + timedelta(days=35)
    chain = OptionsChain(ticker="T", spot=20.0, expirations=[exp])
    chain.puts = [
        _contract(19.0, 0.45, 0.47, 0.46, 50, exp),    # OI too low
        _contract(18.0, 0.30, 0.31, 0.305, 900, exp),  # OI fine, tight spread
    ]
    chosen, warns = _pick_best_put(chain, exp, 20.0)
    assert chosen is not None
    assert chosen.strike == 18.0  # low-OI contract rejected


def test_pick_best_put_still_enforces_spread_on_live_quotes():
    exp = datetime.now() + timedelta(days=35)
    chain = OptionsChain(ticker="T", spot=20.0, expirations=[exp])
    # 30% spread on live quotes → reject; nothing else → None
    chain.puts = [_contract(19.0, 0.30, 0.50, 0.40, 900, exp)]
    chosen, warns = _pick_best_put(chain, exp, 20.0)
    assert chosen is None


# ---------------------------------------------------------------------------
# Friction modeling — virtual PnL must be NET of commission + slippage
# ---------------------------------------------------------------------------

def test_close_pnl_is_net_of_friction():
    s = _setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_f")
    rec = virtual_tracker.close_virtual_position(
        trade_id=tid, exit_reason="test", exit_spot=21.0,
        final_put_price=20.0, credit_received=50.0,
        ticker="TEST", strike=19.0, expiration=s.expiration,
    )
    # gross = 50 - 20 = 30
    # friction = 2*0.65 + 0.05*(50+20) = 1.30 + 3.50 = 4.80
    assert rec["pnl_gross"] == 30.0
    assert rec["friction"] == pytest.approx(4.80)
    assert rec["pnl"] == pytest.approx(25.20)
    # pct is computed on NET
    assert rec["pnl_pct_of_credit"] == pytest.approx(25.20 / 50.0, abs=0.001)


def test_iv_blend_prices_put_higher_than_rv_alone():
    """
    With iv_at_open above current realized vol, the blended sigma must
    produce a HIGHER put mark than RV alone — otherwise the vol risk
    premium is ignored and take-profits fire too early.
    """
    s_rv_only = _setup(ticker="RVONLY", iv=None)      # no IV at open
    s_blend = _setup(ticker="BLEND", iv=0.90)         # rich IV at open
    virtual_tracker.open_virtual_position(s_rv_only, "screen_iv")
    virtual_tracker.open_virtual_position(s_blend, "screen_iv")

    trades = {t.ticker: t for t in virtual_tracker.get_open_virtual_trades()}
    low_iv = 0.20  # current realized vol

    res_rv = virtual_tracker.evaluate_open_position(trades["RVONLY"], 20.0, low_iv)
    res_blend = virtual_tracker.evaluate_open_position(trades["BLEND"], 20.0, low_iv)
    assert res_blend["current_put_price"] > res_rv["current_put_price"]


def test_setup_quality_flag_gets_unverified_suffix():
    from csp_screener.setup_generator import generate_setup
    exp = datetime.now() + timedelta(days=35)
    chain = OptionsChain(ticker="T", spot=20.0, expirations=[exp])
    chain.puts = [_contract(19.0, 0, 0, 0.50, 800, exp)]  # stale quote
    setup = generate_setup("T", 20.0, chain)
    assert setup is not None
    assert setup.data_quality.endswith("_unverified_liquidity")
    assert any("STALE QUOTE" in r for r in setup.reasoning)
