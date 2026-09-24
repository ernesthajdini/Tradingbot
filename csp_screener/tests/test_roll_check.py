"""
Roll evaluator — the arithmetic, and the disclosures the owner's spec makes
mandatory. Reconstructs his real 21 Aug 2026 NVDA roll as the reference case.
"""
from datetime import date, datetime, timedelta

import pytest

from csp_screener import roll_check as rc
from csp_screener.options_data import OptionContract


def C(strike, bid, ask, exp, oi=800):
    return OptionContract("NVDA", exp, strike, "P", bid, ask, (bid + ask) / 2,
                          (bid + ask) / 2, oi, 500, 0.5, -0.3, "test",
                          datetime.now() - timedelta(hours=1), None)


def _chains(cur_exp, next_exp, cur_strike=217.5, cur_bid=2.18, cur_ask=2.23):
    return {
        cur_exp: {"puts": [C(cur_strike, cur_bid, cur_ask, cur_exp)], "calls": []},
        next_exp: {"puts": [C(215, 4.40, 4.50, next_exp),
                            C(210, 4.02, 4.12, next_exp),
                            C(205, 1.80, 1.90, next_exp)], "calls": []},
    }


@pytest.fixture
def aug21(monkeypatch):
    """The owner's real trade: short 4x 217.5P, bought back at 2.23,
    rolled into 28AUG 210P at 4.02."""
    cur = datetime(2026, 8, 21)
    nxt = datetime(2026, 8, 28)
    monkeypatch.setattr(rc, "fetch_weekly_chain",
                        lambda *a, **k: (215.50, _chains(cur, nxt)))
    return cur, nxt


# ---------------------------------------------------------------------------
# the reference case
# ---------------------------------------------------------------------------

def test_realised_loss_matches_the_real_trade(aug21):
    cur, _ = aug21
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4)
    close = [c for c in r["choices"] if c["label"].startswith("D.")][0]
    # (1.07 - 2.23) * 4 * 100 = -464, minus 2 legs x 4 contracts x $1 = -472
    assert close["realised_now"] == pytest.approx(-472.0, abs=0.01)


def test_roll_shows_net_credit_and_does_not_erase_the_loss(aug21):
    cur, nxt = aug21
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4)
    roll = [c for c in r["choices"] if "ROLL to" in c["label"]]
    assert roll, "a net-credit roll should be available here"
    best = [c for c in roll if "210" in c["label"]][0]
    # net credit = (4.02 - 2.23) * 4 * 100 = 716
    assert "716" in best["detail"].replace(",", "")
    # the realised loss is carried, not erased
    assert best["realised_now"] == pytest.approx(-472.0, abs=0.01)
    assert best["sequence_pnl"] == pytest.approx(-472.0, abs=0.01)
    assert any("does NOT erase" in n for n in best["notes"])


def test_sequence_pnl_accumulates_prior_legs(aug21):
    cur, _ = aug21
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4,
                         prior_pnl=-469.54)
    close = [c for c in r["choices"] if c["label"].startswith("D.")][0]
    assert close["sequence_pnl"] == pytest.approx(-469.54 - 472.0, abs=0.01)


def test_new_exposure_is_disclosed_and_compared(aug21):
    cur, _ = aug21
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4)
    best = [c for c in r["choices"] if "210" in c["label"]][0]
    assert best["exposure"] == 210 * 4 * 100
    assert any("New assignment exposure" in n for n in best["notes"])
    assert any("lower" in n for n in best["notes"])


# ---------------------------------------------------------------------------
# the four choices, always present, never a recommendation
# ---------------------------------------------------------------------------

def test_all_four_choices_are_offered(aug21):
    cur, _ = aug21
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4)
    labels = " ".join(c["label"] for c in r["choices"])
    for prefix in ("A. HOLD", "B. ACCEPT ASSIGNMENT", "C. ROLL", "D. CLOSE"):
        assert prefix in labels


def test_report_makes_no_recommendation(aug21):
    cur, _ = aug21
    text = rc.format_report(
        rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4))
    assert "no recommendation is made" in text
    # strip the disclaimer itself before checking the body for advice language
    body = text.lower().replace("no recommendation is made", "")
    for word in ("you should", "recommend", "best choice", "i suggest"):
        assert word not in body


def test_roll_unavailable_is_stated_plainly(monkeypatch):
    cur, nxt = datetime(2026, 8, 21), datetime(2026, 8, 28)
    ch = _chains(cur, nxt)
    ch[nxt]["puts"] = [C(200, 0.40, 0.50, nxt)]      # nothing pays > buyback
    monkeypatch.setattr(rc, "fetch_weekly_chain", lambda *a, **k: (215.50, ch))
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4)
    roll = [c for c in r["choices"] if c["label"].startswith("C.")][0]
    assert roll["action"] == "NOT AVAILABLE"
    assert any("sustained decline" in n for n in roll["notes"])


# ---------------------------------------------------------------------------
# honesty about missing data
# ---------------------------------------------------------------------------

def test_refuses_when_no_two_sided_quote(monkeypatch):
    cur, nxt = datetime(2026, 8, 21), datetime(2026, 8, 28)
    ch = _chains(cur, nxt, cur_bid=0.0, cur_ask=0.0)
    monkeypatch.setattr(rc, "fetch_weekly_chain", lambda *a, **k: (215.50, ch))
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4)
    assert r["error"] and "not a tradable price" in r["error"].lower()
    assert r["choices"] == []


def test_manual_buyback_override_works_when_market_shut(monkeypatch):
    cur, nxt = datetime(2026, 8, 21), datetime(2026, 8, 28)
    ch = _chains(cur, nxt, cur_bid=0.0, cur_ask=0.0)
    monkeypatch.setattr(rc, "fetch_weekly_chain", lambda *a, **k: (215.50, ch))
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4,
                         buyback_override=2.23)
    assert not r["error"]
    assert r["buyback_source"] == "supplied by you"


def test_flags_exposure_beyond_available_cash(aug21):
    cur, _ = aug21
    r = rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4,
                         cash=30000)
    assign = [c for c in r["choices"] if c["label"].startswith("B.")][0]
    assert any("WARNING" in n for n in assign["notes"])


def test_report_carries_the_amendment_17_evidence(aug21):
    cur, _ = aug21
    text = rc.format_report(
        rc.evaluate_roll("NVDA", 217.5, cur.date(), credit=1.07, contracts=4))
    assert "Amendment 17" in text and "FAILED validation" in text


def test_no_order_placement_code_exists():
    import inspect
    src = inspect.getsource(rc)
    for forbidden in ("placeOrder", "place_order", "MarketOrder", "LimitOrder"):
        assert forbidden not in src
