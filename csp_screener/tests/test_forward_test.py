"""
Forward test (Amendment 18) — the expiry rules, the fills, the bar, and the
isolation. The roll arm's reference case is the owner's real 21 Aug 2026
roll: 217.5P bought back at the 2.23 ask, rolled to the 28 Aug 210P.
"""
from datetime import date, datetime, timedelta

import pytest

from csp_screener import book, journal
from csp_screener import forward_test as ft
from csp_screener.options_data import OptionContract
from csp_screener.weekly_csp import Decision

ET = ft.ET
AUG21, AUG28 = datetime(2026, 8, 21), datetime(2026, 8, 28)


@pytest.fixture(autouse=True)
def tmp_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILES",
                        {t: tmp_path / f"{t}.jsonl" for t in journal.JOURNAL_FILES})
    yield


def P(strike, bid, ask, exp, oi=800):
    return OptionContract("NVDA", exp, strike, "P", bid, ask, (bid + ask) / 2,
                          (bid + ask) / 2, oi, 500, 0.5, -0.3, "test",
                          datetime.now(), None)


def chains_aug21(next_puts=None):
    return {
        AUG21: {"puts": [P(217.5, 2.18, 2.23, AUG21)], "calls": []},
        AUG28: {"puts": next_puts if next_puts is not None else
                [P(215, 4.40, 4.50, AUG28), P(210, 4.02, 4.12, AUG28),
                 P(205, 1.80, 1.90, AUG28)], "calls": []},
    }


def open_both(strike=217.5, exp="2026-08-21", credit=1.07):
    for t in ft.ARMS.values():
        book.record_open("NVDA", strike, exp, credit, 1, topic=t)


def decision(verdict="EXECUTE", strike=210.0, exp="2026-08-28", bid=1.00, ask=1.06):
    return Decision("NVDA", verdict, "test", current_price=225.0,
                    expiration=exp, strike=strike, bid=bid, ask=ask,
                    proposed_limit=round((bid + ask) / 2, 2), delta=-0.2, iv=0.4)


# ---------------------------------------------------------------------------
# expiry — the two arms differ only here
# ---------------------------------------------------------------------------

def test_otm_at_expiry_expires_worthless_in_both_arms():
    open_both()
    for arm, t in ft.ARMS.items():
        ft.manage(arm, t, AUG21.date(), 220.0, chains_aug21())
        s = book.replay(t)["NVDA-20260821-217.5"]
        assert s.status == "closed" and s.legs[0].expired_worthless
        assert s.realised == pytest.approx(1.07 * 100 - 1)


def test_itm_take_loss_buys_back_at_the_ask():
    open_both()
    ft.manage("take_loss", ft.ARMS["take_loss"], AUG21.date(), 215.50, chains_aug21())
    s = book.replay(ft.ARMS["take_loss"])["NVDA-20260821-217.5"]
    assert s.status == "closed"
    assert s.legs[0].buyback == 2.23                      # ask, not mid
    assert s.realised == pytest.approx((1.07 - 2.23) * 100 - 2)


def test_itm_roll_arm_reproduces_the_owners_21_aug_roll():
    open_both()
    ft.manage("roll", ft.ARMS["roll"], AUG21.date(), 215.50, chains_aug21())
    s = book.replay(ft.ARMS["roll"])["NVDA-20260821-217.5"]
    assert s.status == "open" and s.rolls == 1
    assert s.current.strike == 210                         # what he chose
    assert s.current.credit == 4.02                        # at the bid
    assert s.current.expiration == "2026-08-28"
    assert s.realised == pytest.approx((1.07 - 2.23) * 100 - 2)   # loss carried


def test_roll_arm_takes_the_loss_when_no_net_credit_exists():
    open_both()
    ch = chains_aug21(next_puts=[P(205, 1.80, 1.90, AUG28)])   # nothing > 2.23
    ft.manage("roll", ft.ARMS["roll"], AUG21.date(), 215.50, ch)
    s = book.replay(ft.ARMS["roll"])["NVDA-20260821-217.5"]
    assert s.status == "closed" and "no net-credit roll" in s.outcome


def test_roll_target_skips_an_expiry_that_spans_earnings():
    tgt = ft.roll_target(chains_aug21(), 215.50, 2.23, AUG21.date(),
                         next_earnings=datetime(2026, 8, 26))
    assert tgt is None


def test_no_two_sided_quote_falls_back_to_intrinsic():
    open_both()
    ch = chains_aug21()
    ch[AUG21]["puts"] = [P(217.5, 0.0, 0.0, AUG21)]
    ft.manage("take_loss", ft.ARMS["take_loss"], AUG21.date(), 215.00, ch)
    s = book.replay(ft.ARMS["take_loss"])["NVDA-20260821-217.5"]
    assert s.legs[0].buyback == pytest.approx(2.50)


def test_before_expiry_nothing_happens():
    open_both()
    for arm, t in ft.ARMS.items():
        assert ft.manage(arm, t, date(2026, 8, 19), 200.0, chains_aug21()) == []
        assert book.has_open("NVDA", t)


# ---------------------------------------------------------------------------
# a missed expiry, and the after-the-decision cross
# ---------------------------------------------------------------------------

def test_missed_expiry_settles_on_the_official_close():
    open_both()
    t = ft.ARMS["roll"]
    ft.manage("roll", t, date(2026, 8, 24), 214.0, {},
              closes={date(2026, 8, 21): 215.0})
    s = book.replay(t)["NVDA-20260821-217.5"]
    assert s.status == "closed" and s.rolls == 0           # no roll possible late
    assert s.legs[0].buyback == pytest.approx(2.50)


def test_missed_expiry_without_a_close_stays_open():
    open_both()
    t = ft.ARMS["take_loss"]
    msgs = ft.manage("take_loss", t, date(2026, 8, 24), 214.0, {}, closes={})
    assert book.has_open("NVDA", t) and "left open" in msgs[0]


def test_put_let_go_but_closed_itm_books_the_assignment_once():
    open_both()
    t = ft.ARMS["take_loss"]
    ft.manage("take_loss", t, AUG21.date(), 218.0, chains_aug21())   # OTM at 14:30
    closes = {AUG21.date(): 216.0}                                    # ITM at 16:00
    assert ft.reconcile(t, closes, AUG21.date()) == []                # not final yet
    msgs = ft.reconcile(t, closes, date(2026, 8, 24))
    assert msgs and "ADJUSTED" in msgs[0]
    assert ft.reconcile(t, closes, date(2026, 8, 25)) == []           # only once
    sb = ft.scoreboard(t)
    assert sb["total"] == pytest.approx(1.07 * 100 - 1 - 1.5 * 100)
    assert sb["adjusted"] == 1


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------

def test_open_fills_at_the_bid_not_the_mid():
    t = ft.ARMS["take_loss"]
    msg = ft.open_if_flat(t, decision(bid=1.00, ask=1.06))
    s = next(iter(book.open_sequences(t).values()))
    assert s.current.credit == 1.00 and "BID" in msg


def test_no_open_on_wait_or_reject_or_when_already_short():
    t = ft.ARMS["roll"]
    assert ft.open_if_flat(t, decision(verdict="WAIT")) is None
    assert ft.open_if_flat(t, decision(verdict="REJECT")) is None
    ft.open_if_flat(t, decision())
    assert ft.open_if_flat(t, decision(strike=205.0, exp="2026-09-04")) is None


# ---------------------------------------------------------------------------
# a whole run
# ---------------------------------------------------------------------------

@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(ft, "_fetch_chain", lambda keep=(): (215.50, chains_aug21()))
    monkeypatch.setattr(ft, "_evaluate", lambda: decision())
    monkeypatch.setattr(ft, "_next_earnings", lambda: None)
    monkeypatch.setattr(ft, "_closes_since", lambda start: {})


def test_run_on_expiry_day_manages_then_reopens_same_day(live):
    open_both()
    res = ft.run(now=datetime(2026, 8, 21, 14, 30, tzinfo=ET))
    assert res["acted"]
    tl = book.replay(ft.ARMS["take_loss"])
    assert tl["NVDA-20260821-217.5"].status == "closed"
    assert book.has_open("NVDA", ft.ARMS["take_loss"])       # fresh sequence
    rl = book.open_sequences(ft.ARMS["roll"])
    assert list(rl) == ["NVDA-20260821-217.5"]               # rolled, not reopened


def test_run_is_once_per_day_and_only_in_the_window(live):
    assert ft.run(now=datetime(2026, 8, 21, 10, 0, tzinfo=ET))["reason"].startswith("outside")
    assert ft.run(now=datetime(2026, 8, 22, 14, 30, tzinfo=ET))["reason"].startswith("outside")
    assert ft.run(now=datetime(2026, 8, 21, 14, 30, tzinfo=ET))["acted"]
    again = ft.run(now=datetime(2026, 8, 21, 15, 30, tzinfo=ET))
    assert not again["acted"] and again["reason"].startswith("already")


def test_data_outage_is_recorded_and_retried(monkeypatch, live):
    monkeypatch.setattr(ft, "_fetch_chain", lambda keep=(): (None, {}))
    res = ft.run(now=datetime(2026, 8, 21, 14, 30, tzinfo=ET))
    assert not res["acted"] and "no live NVDA price" in res["reason"]
    monkeypatch.setattr(ft, "_fetch_chain", lambda keep=(): (215.50, chains_aug21()))
    assert ft.run(now=datetime(2026, 8, 21, 15, 30, tzinfo=ET))["acted"]


def test_writes_only_its_own_topics(live):
    open_both()
    ft.run(now=datetime(2026, 8, 21, 14, 30, tzinfo=ET))
    assert journal.read_all("weekly_book") == []          # the owner's real book
    assert journal.read_all("virtual_trades") == []       # the go-live record
    assert journal.read_all("paper_orders") == []


# ---------------------------------------------------------------------------
# the bar
# ---------------------------------------------------------------------------

def _sb(pnls):
    tot = sum(pnls)
    import statistics
    return {"n": len(pnls), "total": tot, "mean": tot / len(pnls),
            "median": statistics.median(pnls),
            "top_share": max(pnls) / tot if tot > 0 else None}


def test_bar_is_not_readable_before_its_conditions():
    s, why = ft.bar_status(_sb([100.0] * 30), weeks=10, drop=-12.0)
    assert s == "NOT YET" and any("weeks" in x for x in why)
    s, why = ft.bar_status(_sb([100.0] * 30), weeks=30, drop=-6.0)
    assert s == "NOT YET" and any("NVDA week" in x for x in why)


def test_bar_kill_line_fails_at_once():
    s, _ = ft.bar_status(_sb([100.0, -3200.0]), weeks=2, drop=-1.0)
    assert s == "FAILED"


def test_bar_clears_only_with_positive_mean_and_median():
    assert ft.bar_status(_sb([100.0] * 30), 30, -12.0)[0] == "CLEARED"
    assert ft.bar_status(_sb([100.0] * 29 + [-3500.0]), 30, -12.0)[0] == "FAILED"


def test_roll_must_beat_taking_the_loss():
    ctrl = _sb([100.0] * 30)
    s, why = ft.bar_status(_sb([90.0] * 30), 30, -12.0, control=ctrl)
    assert s == "FAILED" and any("did not beat" in x for x in why)


def test_worst_5d_drop():
    closes = {date(2026, 10, 9) + timedelta(days=i): px for i, px in
              enumerate([100, 101, 102, 100, 95, 90, 92])}
    # 5 trading days apart: 100 -> 90. (102 -> 90 is only three.)
    assert ft.worst_5d_drop(closes) == pytest.approx(-10.0)
    # exactly 10% satisfies the ">= 10% week" condition
    assert ft.bar_status(_sb([100.0] * 30), 30, -10.0)[0] == "CLEARED"


def test_report_says_not_advice_and_states_the_bar(live):
    open_both()
    res = ft.run(now=datetime(2026, 8, 21, 14, 30, tzinfo=ET))
    subj, text, _ = ft.build_report(res, {}, chains_aug21(), today=date(2026, 10, 9))
    assert "CAN I TRADE IT YET?" in text and "NOT YET" in subj
    assert "Not advice" in text
    assert f">= {ft.BAR_MIN_SEQUENCES}" in text


def test_no_order_placement_code_exists():
    import inspect
    src = inspect.getsource(ft)
    for forbidden in ("placeOrder", "place_order", "MarketOrder", "LimitOrder",
                      "ibkr_paper", "ib_insync"):
        assert forbidden not in src


def test_unpriceable_straddle_in_market_hours_is_an_outage_not_a_decision(monkeypatch, live):
    monkeypatch.setattr(ft, "_evaluate", lambda: Decision(
        "NVDA", "REJECT", "cannot price the ATM straddle (no strike with both sides quoted)"))
    res = ft.run(now=datetime(2026, 8, 21, 14, 30, tzinfo=ET))
    assert not res["acted"] and "retried" in res["reason"]
    monkeypatch.setattr(ft, "_evaluate", lambda: decision())
    assert ft.run(now=datetime(2026, 8, 21, 15, 30, tzinfo=ET))["acted"]


def test_a_real_rule_rejection_counts_as_the_days_run(monkeypatch, live):
    monkeypatch.setattr(ft, "_evaluate", lambda: Decision(
        "NVDA", "REJECT", "earnings 2026-08-26 falls before expiry — the spec rejects this by default"))
    assert ft.run(now=datetime(2026, 8, 21, 14, 30, tzinfo=ET))["acted"]
