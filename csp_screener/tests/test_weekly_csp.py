"""
Weekly CSP signal — the spec's arithmetic and every mandatory gate, against
synthetic chains so no test touches the network.
"""
from datetime import datetime, timedelta

import pytest

from csp_screener import weekly_csp as w
from csp_screener.options_data import OptionContract


def C(strike, bid, ask, right="P", oi=500, exp=None, delta=None):
    return OptionContract(
        ticker="TEST", expiration=exp or (datetime.now() + timedelta(days=7)),
        strike=strike, right=right, bid=bid, ask=ask, last=(bid + ask) / 2,
        mid=(bid + ask) / 2, open_interest=oi, volume=100, iv=0.45,
        delta=delta, source="test",
        last_trade_date=datetime.now() - timedelta(hours=1))


# ---------------------------------------------------------------------------
# expected move
# ---------------------------------------------------------------------------

def test_straddle_uses_strike_nearest_spot():
    puts = [C(220, 3.0, 3.2), C(225, 5.0, 5.2), C(230, 8.0, 8.2)]
    calls = [C(220, 8.0, 8.2, "C"), C(225, 5.0, 5.2, "C"), C(230, 3.0, 3.2, "C")]
    cm, pm, k = w.atm_straddle(puts, calls, 225.16)
    assert k == 225
    assert cm == pytest.approx(5.1) and pm == pytest.approx(5.1)


def test_straddle_needs_both_sides_two_sided():
    puts = [C(225, 5.0, 5.2)]
    calls = [C(225, 0.0, 5.2, "C")]            # no bid
    assert w.atm_straddle(puts, calls, 225.0) is None


def test_straddle_none_when_no_shared_strike():
    assert w.atm_straddle([C(225, 5, 5.2)], [C(230, 5, 5.2, "C")], 225.0) is None


# ---------------------------------------------------------------------------
# strike selection — the owner's example contradicts his own rule
# ---------------------------------------------------------------------------

def test_picks_highest_strike_at_or_below_reference():
    puts = [C(210, 0.9, 1.0), C(215, 1.5, 1.6), C(217.5, 2.0, 2.1)]
    got = w.select_strike(puts, 216.02)
    assert got.strike == 215          # 217.5 is ABOVE the reference


def test_rejects_illiquid_and_one_sided_strikes():
    puts = [C(215, 1.5, 1.6, oi=10), C(212.5, 0.0, 1.2), C(210, 0.9, 1.0)]
    assert w.select_strike(puts, 216.0).strike == 210


def test_none_when_nothing_below_reference():
    assert w.select_strike([C(220, 3, 3.2)], 216.0) is None


# ---------------------------------------------------------------------------
# full evaluation
# ---------------------------------------------------------------------------

def _chain(spot=225.16, strikes=(205, 210, 215, 217.5, 220, 225, 230),
           oi=500, put_bid=None):
    exp = datetime.now() + timedelta(days=7)
    puts, calls = [], []
    for k in strikes:
        pb = put_bid if put_bid is not None else max(0.2, (225 - k) * 0.02 + 1.0)
        puts.append(C(k, pb, pb + 0.05, oi=oi, exp=exp))
        cb = max(0.2, (k - 225) * -0.02 + 5.0)
        calls.append(C(k, cb, cb + 0.05, "C", oi=oi, exp=exp))
    return spot, {exp: {"puts": puts, "calls": calls}}


@pytest.fixture
def no_earnings(monkeypatch):
    import csp_screener.earnings as e
    monkeypatch.setattr(e, "fetch_next_earnings",
                        lambda t: datetime.now() + timedelta(days=60))


def test_execute_when_every_gate_passes(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: _chain())
    d = w.evaluate("TEST", cash=30000)
    assert d.verdict == "EXECUTE"
    assert d.strike < d.current_price
    assert d.cash_if_assigned == d.strike * 100
    # effective acquisition and buffer are internally consistent
    assert d.effective_acquisition == pytest.approx(d.strike - d.proposed_limit)
    assert d.downside_buffer_pct == pytest.approx(
        100 * (d.current_price - d.effective_acquisition) / d.current_price, abs=0.01)


def test_rejects_when_exposure_exceeds_cash(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: _chain())
    d = w.evaluate("TEST", cash=5000)
    assert d.verdict == "REJECT" and "assignment exposure" in d.reason


def test_reserve_is_honoured(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: _chain())
    ok = w.evaluate("TEST", cash=30000, reserve=0)
    blocked = w.evaluate("TEST", cash=30000, reserve=25000)
    assert ok.verdict == "EXECUTE" and blocked.verdict == "REJECT"


def test_rejects_earnings_before_expiry_by_default(monkeypatch):
    import csp_screener.earnings as e
    monkeypatch.setattr(e, "fetch_next_earnings",
                        lambda t: datetime.now() + timedelta(days=3))
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: _chain())
    d = w.evaluate("TEST", cash=30000)
    assert d.verdict == "REJECT" and "earnings" in d.reason
    # ...and can be overridden explicitly
    assert w.evaluate("TEST", cash=30000, allow_earnings=True).verdict == "EXECUTE"


def test_unknown_earnings_is_reported_not_assumed(monkeypatch, no_earnings):
    import csp_screener.earnings as e
    monkeypatch.setattr(e, "fetch_next_earnings", lambda t: None)
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: _chain())
    d = w.evaluate("TEST", cash=30000)
    assert "UNKNOWN" in d.earnings_before_expiry


def test_rejects_illiquid_chain(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: _chain(oi=5))
    d = w.evaluate("TEST", cash=30000)
    assert d.verdict == "REJECT" and "at or below" in d.reason


def test_rejects_dust_credit(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain",
                        lambda *a, **k: _chain(put_bid=0.01))
    d = w.evaluate("TEST", cash=30000)
    assert d.verdict == "REJECT"


def test_waits_when_no_expiry_in_window(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: (225.0, {}))
    d = w.evaluate("TEST", cash=30000)
    assert d.verdict == "WAIT"


def test_rejects_when_no_price(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: (None, {}))
    assert w.evaluate("TEST", cash=30000).verdict == "REJECT"


# ---------------------------------------------------------------------------
# output discipline
# ---------------------------------------------------------------------------

def test_output_carries_the_evidence_and_never_says_market(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: _chain())
    text = w.format_decision(w.evaluate("TEST", cash=30000))
    assert "LIMIT" in text and "Never a market order" in text
    assert "Amendment 17" in text          # the tool states its own evidence
    assert "MARKET ORDER" not in text.upper().replace("NEVER A MARKET ORDER", "")


def test_macro_event_is_not_fabricated(monkeypatch, no_earnings):
    monkeypatch.setattr(w, "fetch_weekly_chain", lambda *a, **k: _chain())
    d = w.evaluate("TEST", cash=30000)
    assert "NOT CHECKED" in d.macro_event_before_expiry


def test_no_order_placement_code_exists():
    import inspect
    src = inspect.getsource(w)
    for forbidden in ("placeOrder", "place_order", "MarketOrder", "LimitOrder"):
        assert forbidden not in src
