"""
Tests for profitability-map items #1 and #2:
  - market-hours screen: alert email, honest 'quotes unavailable' labeling,
    near-miss ledger diagnostics
  - market-quote marks: real quotes drive closes, model is the fallback
"""
from datetime import datetime, timedelta

import pytest

from csp_screener import journal, notify, virtual_tracker
from csp_screener.main import us_market_likely_open
from csp_screener.options_data import OptionContract, OptionsChain
from csp_screener.setup_generator import generate_spread_setup
from csp_screener.virtual_tracker import OpenVirtualTrade, evaluate_open_position


@pytest.fixture(autouse=True)
def clean_journal(tmp_path, monkeypatch):
    new_files = {topic: tmp_path / f"{topic}.jsonl" for topic in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", new_files)
    yield


NOW = datetime(2026, 7, 20, 12, 0)
EXP = datetime(2026, 8, 21)
FRESH = NOW - timedelta(days=1)


def _spread_put(strike, mid, delta, spread_pct=0.02):
    half = mid * spread_pct / 2
    return OptionContract(
        ticker="DKNG", expiration=EXP, strike=strike, right="P",
        bid=round(mid - half, 4), ask=round(mid + half, 4), last=mid, mid=mid,
        open_interest=2000, volume=300, iv=0.6, delta=delta,
        source="yfinance", last_trade_date=FRESH,
    )


def _chain(spot, puts):
    return OptionsChain(ticker="DKNG", spot=spot, expirations=[EXP], puts=puts,
                        source="yfinance")


# ---------------------------------------------------------------------------
# Market-hours labeling
# ---------------------------------------------------------------------------

def test_us_market_likely_open_boundaries():
    assert us_market_likely_open(datetime(2026, 7, 20, 14, 0))       # Mon 14:00 UTC
    assert us_market_likely_open(datetime(2026, 7, 20, 15, 5))       # the RTH cron slot
    assert not us_market_likely_open(datetime(2026, 7, 20, 22, 0))   # after close
    assert not us_market_likely_open(datetime(2026, 7, 19, 15, 0))   # Sunday
    assert not us_market_likely_open(datetime(2026, 7, 20, 13, 0))   # pre-open


def test_no_trade_banner_says_quotes_unavailable_when_market_closed():
    html = notify.render_live_section([], True, {"market_open": False})
    assert "QUOTES UNAVAILABLE" in html
    assert "NO TRADE" not in html

    html_open = notify.render_live_section([], True, {"market_open": True})
    assert "NO TRADE" in html_open


# ---------------------------------------------------------------------------
# Near-miss ledger diagnostics
# ---------------------------------------------------------------------------

def test_near_miss_ledger_reports_credit_vs_floor(monkeypatch):
    # $2-wide spreads only fit once equity reaches the balance the
    # $130 cap assumes ($2.6K). This test is about the other gates.
    from csp_screener import account
    monkeypatch.setattr(account, "CURRENT_EQUITY", 2600.0)
    # $1-wide netting $20 gross: risk $80 (under the $130 cap) but only
    # $14 after friction — below the $25 floor. Must produce a NEAR MISS
    # diagnostic with the numbers, not a silent void.
    chain = _chain(25.0, [
        _spread_put(23.0, 0.30, -0.25),
        _spread_put(22.0, 0.10, -0.15),
    ])
    diags: list = []
    setup = generate_spread_setup("DKNG", 25.0, chain, diagnostics=diags)
    assert setup is None
    assert any("NEAR MISS" in d for d in diags)


def test_near_miss_ledger_reports_risk_cap(monkeypatch):
    # $2-wide spreads only fit once equity reaches the balance the
    # $130 cap assumes ($2.6K). This test is about the other gates.
    from csp_screener import account
    monkeypatch.setattr(account, "CURRENT_EQUITY", 2600.0)
    # $2-wide netting $55: friction passes but risk $145 > the $130 cap —
    # the ledger must say so with numbers.
    chain = _chain(25.0, [
        _spread_put(23.0, 0.75, -0.28),
        _spread_put(21.0, 0.20, -0.12),
    ])
    diags: list = []
    setup = generate_spread_setup("DKNG", 25.0, chain, diagnostics=diags)
    assert setup is None
    assert any("cap" in d and "$145" in d for d in diags)


def test_live_path_delta_cap_rejects_near_money_short_leg():
    # Red-team finding: the 0.40 delta cap existed only in the sandbox path.
    chain = _chain(25.0, [
        _spread_put(24.5, 2.60, -0.48),  # rule-breaking near-ATM
        _spread_put(22.0, 0.90, -0.30),  # nothing below it for a long leg
    ])
    diags: list = []
    setup = generate_spread_setup("DKNG", 25.0, chain, diagnostics=diags)
    # The 24.5 leg must never be the short leg; the 22.0 has no long leg
    # below it in this chain, so the result is a clean no-trade.
    assert setup is None or setup.strike != 24.5


def test_viable_spread_still_generates(monkeypatch):
    # $2-wide spreads only fit once equity reaches the balance the
    # $130 cap assumes ($2.6K). This test is about the other gates.
    from csp_screener import account
    monkeypatch.setattr(account, "CURRENT_EQUITY", 2600.0)
    # $2-wide netting $75: risk $125 ≤ $130 cap, friction $11.50 ≤ 20% of
    # credit, net $63.50 ≥ $25 floor — every gate passes.
    chain = _chain(26.0, [
        _spread_put(24.0, 0.95, -0.28),
        _spread_put(22.0, 0.20, -0.12),
    ])
    diags: list = []
    setup = generate_spread_setup("DKNG", 26.0, chain, diagnostics=diags)
    assert setup is not None
    assert setup.strike == 24.0 and setup.long_strike == 22.0


# ---------------------------------------------------------------------------
# Market-quote marks
# ---------------------------------------------------------------------------

def _trade(credit=50.0, iv=0.6):
    return OpenVirtualTrade(
        trade_id="t::WEN::6.0::2026-08-21", screen_id="s",
        opened_at=NOW - timedelta(days=3), ticker="WEN", spot_at_open=8.0,
        expiration=EXP, dte_at_open=35, strike=6.0,
        credit_received=credit, max_loss=550.0, breakeven=5.5,
        iv_at_open=iv,
    )


def test_market_price_overrides_model_mark():
    r = evaluate_open_position(_trade(), current_spot=8.0, current_iv=0.6,
                               today=NOW, market_price=20.0)
    assert r["mark_source"] == "market"
    assert r["current_put_price"] == 20.0
    assert r["model_put_price"] != 20.0  # model still computed for comparison
    assert r["pnl"] == pytest.approx(30.0)


def test_model_fallback_when_no_quote():
    r = evaluate_open_position(_trade(), current_spot=8.0, current_iv=0.6,
                               today=NOW, market_price=None)
    assert r["mark_source"] == "model"
    assert r["current_put_price"] == r["model_put_price"]


def test_update_all_uses_quote_resolver_and_stamps_source():
    from csp_screener.setup_generator import VirtualSetup
    # Credit kept under the deep-OTM sanity cap (8% of strike at |Δ|<0.15):
    # $40 on a $6 strike = 6.7% — a legal trade the quarantine must keep.
    s = VirtualSetup(
        ticker="WEN", spot_at_screen=8.0,
        expiration=EXP.date().isoformat(), dte=35, strike=6.0,
        pct_otm=0.25, delta=-0.10, iv=0.6,
        bid=0.39, ask=0.41, mid=0.40, bid_ask_pct=0.05,
        open_interest=1000, volume=100,
        estimated_credit_per_contract=40.0, max_loss_per_contract=560.0,
        breakeven=5.6, data_quality="ibkr_greeks", reasoning=[],
    )
    virtual_tracker.open_virtual_position(s, "screen_q")

    # Market quote says the put now trades at $20 → 50% of credit captured
    # → take-profit fires FROM THE MARKET PRICE, not the model.
    summary = virtual_tracker.update_all_open_positions(
        spot_resolver=lambda t: 8.0,
        iv_resolver=lambda t: 0.6,
        quote_resolver=lambda tr: 20.0,
    )
    assert summary["closed"] == 1
    assert summary["details"][0]["mark_source"] == "market"
    closes = journal.read_filtered("virtual_trades", event="close")
    assert "mark_source=market" in closes[0]["notes"]
    assert closes[0]["final_put_price"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# fetch_position_quote gates (critical adversarial-review finding, verified
# live: after the close yfinance zeroes bid/ask, mid falls back to lastPrice,
# and two legs' NON-SYNCHRONOUS lasts inverted a spread to a fake +100% win)
# ---------------------------------------------------------------------------

def _cached_put(strike, bid, ask, last):
    from csp_screener.options_data import OptionContract
    mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
    return OptionContract(
        ticker="RIVN", expiration=EXP, strike=strike, right="P",
        bid=bid, ask=ask, last=last, mid=mid, open_interest=500,
        volume=5, iv=0.8, delta=-0.1, source="yfinance",
        last_trade_date=FRESH,
    )


def test_position_quote_rejects_last_price_fallback():
    from csp_screener import options_data
    options_data._position_quote_cache[("RIVN", "2099-01-01")] = {
        11.5: _cached_put(11.5, 0, 0, 0.03),   # zeroed book, stale last
        11.0: _cached_put(11.0, 0, 0, 0.06),   # inverted vs the 11.5
    }
    assert options_data.fetch_position_quote("RIVN", "2099-01-01", 11.5, 11.0) is None
    assert options_data.fetch_position_quote("RIVN", "2099-01-01", 11.5) is None


def test_position_quote_rejects_inverted_live_legs():
    from csp_screener import options_data
    options_data._position_quote_cache[("RIVN", "2099-01-03")] = {
        11.5: _cached_put(11.5, 0.02, 0.04, 0.03),  # live but inverted pair
        11.0: _cached_put(11.0, 0.05, 0.07, 0.06),
    }
    # Negative spread value = inconsistent quotes → None, NEVER clamp to $0
    # (a $0 mark is a fake full-profit close).
    assert options_data.fetch_position_quote("RIVN", "2099-01-03", 11.5, 11.0) is None


def test_position_quote_accepts_live_tight_quotes():
    from csp_screener import options_data
    options_data._position_quote_cache[("RIVN", "2099-01-02")] = {
        11.5: _cached_put(11.5, 0.28, 0.30, 0.29),
        11.0: _cached_put(11.0, 0.18, 0.20, 0.19),
    }
    q = options_data.fetch_position_quote("RIVN", "2099-01-02", 11.5, 11.0)
    assert q == pytest.approx(10.0, abs=0.01)


def test_position_quote_rejects_wide_auction_quotes():
    from csp_screener import options_data
    # 0.05/3.00 at the open — the whipsaw stop-loss scenario
    options_data._position_quote_cache[("RIVN", "2099-01-04")] = {
        11.5: _cached_put(11.5, 0.05, 3.00, 0.50),
    }
    assert options_data.fetch_position_quote("RIVN", "2099-01-04", 11.5) is None


# ---------------------------------------------------------------------------
# Friction-viability entry gate (the negative-take-profit incident: T puts
# collecting $1-2 credit closed as 'take_profit_50pct' with NEGATIVE net
# P&L — 50% of a $2 credit cannot survive the $2 round-trip commission)
# ---------------------------------------------------------------------------

def _csp_chain(ticker, spot, strike, mid, delta=-0.20):
    half = max(0.005, mid * 0.01)
    put = OptionContract(
        ticker=ticker, expiration=EXP, strike=strike, right="P",
        bid=round(mid - half, 4), ask=round(mid + half, 4), last=mid, mid=mid,
        open_interest=2000, volume=100, iv=0.5, delta=delta,
        source="yfinance", last_trade_date=FRESH,
    )
    return OptionsChain(ticker=ticker, spot=spot, expirations=[EXP], puts=[put],
                        source="yfinance")


def test_tiny_credit_csp_rejected_at_entry():
    from csp_screener.setup_generator import generate_setup
    # $2 credit: a perfect 50% TP captures $1 gross vs ~$2.15 friction
    chain = _csp_chain("T", 22.0, 18.0, 0.02)
    assert generate_setup("T", 22.0, chain) is None


def test_healthy_credit_csp_still_generates():
    from csp_screener.setup_generator import generate_setup
    # $30 credit: 50% TP nets ~$10.7 after friction — viable
    chain = _csp_chain("LCID", 7.4, 4.0, 0.30, delta=-0.10)
    setup = generate_setup("LCID", 7.4, chain)
    assert setup is not None


# ---------------------------------------------------------------------------
# Reopen cooldown (T churned open→close→reopen 4x in 4 days)
# ---------------------------------------------------------------------------

def test_reopen_cooldown_blocks_recent_close():
    from csp_screener.main import step_open_virtual_positions
    from csp_screener.setup_generator import VirtualSetup

    journal.append("virtual_trades", {
        "event": "close", "trade_id": "old::T::18.0::2026-08-21", "ticker": "T",
        "closed_at": (datetime.now() - timedelta(days=1)).isoformat(),
        "exit_reason": "take_profit_50pct", "pnl": -0.5,
        "strike": 18.0, "expiration": EXP.date().isoformat(),
    })
    s = VirtualSetup(
        ticker="T", spot_at_screen=22.0, expiration=EXP.date().isoformat(),
        dte=32, strike=18.0, pct_otm=0.18, delta=-0.2, iv=0.5,
        bid=0.29, ask=0.31, mid=0.30, bid_ask_pct=0.06,
        open_interest=2000, volume=100,
        estimated_credit_per_contract=30.0, max_loss_per_contract=1770.0,
        breakeven=17.7, data_quality="ibkr_greeks", reasoning=[],
    )
    opened = step_open_virtual_positions(
        [{"ticker": "T", "setup": s.to_dict()}], "screen_cd")
    assert opened == []  # cooldown blocks the reopen


def test_reopen_allowed_after_cooldown_expires():
    from csp_screener.main import REOPEN_COOLDOWN_DAYS, step_open_virtual_positions
    from csp_screener.setup_generator import VirtualSetup

    journal.append("virtual_trades", {
        "event": "close", "trade_id": "old::T::18.0::2026-08-21", "ticker": "T",
        "closed_at": (datetime.now() - timedelta(days=REOPEN_COOLDOWN_DAYS + 2)).isoformat(),
        "exit_reason": "take_profit_50pct", "pnl": 12.0,
        "strike": 18.0, "expiration": EXP.date().isoformat(),
    })
    s = VirtualSetup(
        ticker="T", spot_at_screen=22.0, expiration=EXP.date().isoformat(),
        dte=32, strike=18.0, pct_otm=0.18, delta=-0.2, iv=0.5,
        bid=0.29, ask=0.31, mid=0.30, bid_ask_pct=0.06,
        open_interest=2000, volume=100,
        estimated_credit_per_contract=30.0, max_loss_per_contract=1770.0,
        breakeven=17.7, data_quality="ibkr_greeks", reasoning=[],
    )
    opened = step_open_virtual_positions(
        [{"ticker": "T", "setup": s.to_dict()}], "screen_cd2")
    assert len(opened) == 1


# ---------------------------------------------------------------------------
# Ticket alert email
# ---------------------------------------------------------------------------

def test_ticket_alert_renders_with_best_pick():
    live = [{
        "ticker": "DKNG", "last_price": 25.89, "rv_percentile": 97.0,
        "tier": "live",
        "setup": {
            "ticker": "DKNG", "spot_at_screen": 25.89,
            "expiration": "2026-08-21", "dte": 32, "strike": 24.0,
            "long_strike": 22.0, "pct_otm": 0.073, "delta": -0.28, "iv": 0.7,
            "bid": 0.55, "ask": 0.60, "mid": 0.58, "bid_ask_pct": 0.02,
            "open_interest": 4200, "volume": 800,
            "estimated_credit_per_contract": 58.0,
            "max_loss_per_contract": 142.0, "breakeven": 23.42,
            "data_quality": "ibkr_greeks", "reasoning": [],
            "structure": "put_credit_spread",
            "net_credit_after_friction": 49.0,
            "ticket": "SELL -1 DKNG ...",
        },
    }]
    flags = {"live_risk_open": 0.0, "budget_cap": 200.0, "slots_used": 0,
             "slots_max": 2, "max_risk_per_spread": 130.0, "market_open": True}
    subject, html = notify.render_ticket_alert(live, flags)
    assert "TICKET STAGED" in subject and "DKNG" in subject
    assert "BEST PICK" in html
    assert "RISK BUDGET" in html
