"""Tests for the two-tier playbook implementation."""
from datetime import datetime, timedelta

import pytest

from csp_screener import config, journal, virtual_tracker
from csp_screener.options_data import OptionContract, OptionsChain
from csp_screener.setup_generator import VirtualSetup, generate_spread_setup


@pytest.fixture(autouse=True)
def clean_journal(tmp_path, monkeypatch):
    new_files = {topic: tmp_path / f"{topic}.jsonl" for topic in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", new_files)
    yield


def _put(strike, bid, ask, oi, exp, delta=None):
    mid = (bid + ask) / 2
    return OptionContract(
        ticker="T", expiration=exp, strike=strike, right="P",
        bid=bid, ask=ask, last=mid, mid=mid,
        open_interest=oi, volume=100, iv=0.40, delta=delta, source="yfinance",
    )


def _spread_chain(spot=25.0):
    exp = datetime.now() + timedelta(days=35)
    chain = OptionsChain(ticker="T", spot=spot, expirations=[exp])
    # Rich-IV chain. The credit floor is now measured at the 50% take-profit
    # (the exit the ticket attaches), so a viable $2-wide needs ~$70 gross:
    # net@TP $25.75 >= $25 floor, and risk $130 <= the structural ceiling.
    chain.puts = [
        _put(24.0, 0.78, 0.80, 2000, exp, delta=-0.30),
        _put(23.0, 0.19, 0.21, 1500, exp, delta=-0.16),
        _put(22.0, 0.08, 0.10, 900, exp, delta=-0.09),
    ]
    return chain


# ---------------------------------------------------------------------------
# Spread generation
# ---------------------------------------------------------------------------

def test_generate_spread_setup_builds_viable_spread():
    chain = _spread_chain()
    s = generate_spread_setup("T", 25.0, chain)
    assert s is not None
    assert s.structure == "put_credit_spread"
    assert s.tier == "live"
    assert s.strike == 24.0
    assert s.long_strike == 22.0
    # Credit = (0.79 - 0.09) * 100 = 70
    assert s.estimated_credit_per_contract == pytest.approx(70.0, abs=1.0)
    # $2-wide chosen: max loss = 200 - 70 = 130 (at the ceiling)
    assert s.max_loss_per_contract == pytest.approx(130.0, abs=1.0)
    assert s.ticket and "SELL -1 T" in s.ticket and "BUY +1 T" in s.ticket
    assert s.net_credit_after_friction is not None
    assert s.net_credit_after_friction >= config.MIN_NET_CREDIT_AFTER_FRICTION


def test_spread_rejected_when_credit_too_small():
    exp = datetime.now() + timedelta(days=35)
    chain = OptionsChain(ticker="T", spot=25.0, expirations=[exp])
    # Tiny credit spread: net ~ $8 -> below $25 gate
    chain.puts = [
        _put(24.0, 0.20, 0.21, 2000, exp, delta=-0.30),
        _put(23.0, 0.12, 0.13, 1500, exp, delta=-0.18),
    ]
    assert generate_spread_setup("T", 25.0, chain) is None


def test_spread_rejected_without_live_quotes():
    exp = datetime.now() + timedelta(days=35)
    chain = OptionsChain(ticker="T", spot=25.0, expirations=[exp])
    # Stale weekend quotes (bid=ask=0): sandbox tolerates, LIVE must not
    c1 = OptionContract(ticker="T", expiration=exp, strike=24.0, right="P",
                        bid=0, ask=0, last=0.99, mid=0.99,
                        open_interest=2000, volume=0, iv=0.4, delta=-0.3,
                        source="yfinance")
    c2 = OptionContract(ticker="T", expiration=exp, strike=23.0, right="P",
                        bid=0, ask=0, last=0.15, mid=0.15,
                        open_interest=1500, volume=0, iv=0.4, delta=-0.18,
                        source="yfinance")
    chain.puts = [c1, c2]
    assert generate_spread_setup("T", 25.0, chain) is None


def test_spread_rejected_when_max_loss_exceeds_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_RISK_PER_SPREAD", 50.0)
    chain = _spread_chain()  # max loss ~66 > 50
    assert generate_spread_setup("T", 25.0, chain) is None


# ---------------------------------------------------------------------------
# Spread tracking (virtual tracker)
# ---------------------------------------------------------------------------

def _spread_virtual_setup():
    chain = _spread_chain()
    return generate_spread_setup("T", 25.0, chain)


def test_spread_open_close_lifecycle_with_structure():
    s = _spread_virtual_setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_s")
    trades = virtual_tracker.get_open_virtual_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t.structure == "put_credit_spread"
    assert t.long_strike == 22.0
    assert t.tier == "live"


def test_spread_value_capped_by_long_leg():
    """Deep ITM: spread value must approach width, never the naked put value."""
    s = _spread_virtual_setup()
    virtual_tracker.open_virtual_position(s, "screen_s")
    t = virtual_tracker.get_open_virtual_trades()[0]
    # Crash spot to $15 — naked 24P would be ~$9; spread caps near $1 width
    res = virtual_tracker.evaluate_open_position(t, current_spot=15.0, current_iv=0.6)
    assert res["current_put_price"] <= 2.0 * 100 + 5  # width + tolerance


def test_spread_friction_uses_four_legs():
    s = _spread_virtual_setup()
    tid = virtual_tracker.open_virtual_position(s, "screen_s")
    t = virtual_tracker.get_open_virtual_trades()[0]
    rec = virtual_tracker.close_virtual_position(
        trade_id=tid, exit_reason="test", exit_spot=26.0,
        final_put_price=10.0, credit_received=t.credit_received,
        ticker=t.ticker, strike=t.strike,
        expiration=t.expiration.date().isoformat(),
        structure=t.structure, tier=t.tier, long_strike=t.long_strike,
        eur_usd_rate=1.10,
    )
    # 4 legs x $1.00 commissions
    expected_commission = 4 * config.COMMISSION_PER_CONTRACT
    slip = config.SLIPPAGE_PCT_OF_PREMIUM * (t.credit_received + 10.0)
    assert rec["friction"] == pytest.approx(expected_commission + slip, abs=0.05)
    assert rec["pnl_pessimistic"] < rec["pnl"]
    assert rec["pnl_eur"] == pytest.approx(rec["pnl"] / 1.10, abs=0.02)
    assert rec["tier"] == "live"


# ---------------------------------------------------------------------------
# FOMC calendar
# ---------------------------------------------------------------------------

def test_fomc_dates():
    from datetime import date
    from csp_screener import fomc
    assert fomc.next_fomc(date(2026, 7, 1)) == date(2026, 7, 29)
    assert fomc.days_to_fomc(date(2026, 7, 28)) == 1
    assert fomc.is_fomc_week(date(2026, 7, 27))
    assert not fomc.is_fomc_week(date(2026, 8, 15))


# ---------------------------------------------------------------------------
# Universe tiers
# ---------------------------------------------------------------------------

def test_universe_tiers_distinct():
    from csp_screener import universe
    live = universe.get_universe("live")
    sandbox = universe.get_universe("sandbox")
    assert len(live) > 20
    assert len(sandbox) > 50
    both = universe.all_tickers()
    assert len(both) == len(set(both))  # no dupes in union
