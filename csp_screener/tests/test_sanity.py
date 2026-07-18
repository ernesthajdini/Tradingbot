"""
Tests for the credit-sanity layer (csp_screener/sanity.py).

Pinned against the real production incident: LCID $3.50 put, delta -0.12,
$175.50 credit from a misaligned yfinance row — booked fake +$77 wins daily
and produced a fraudulent 100% win rate.
"""
from datetime import datetime, timedelta

import pytest

from csp_screener import evaluator, journal, virtual_tracker
from csp_screener.sanity import (
    csp_credit_is_sane,
    open_event_is_sane,
    spread_credit_is_sane,
)


@pytest.fixture(autouse=True)
def clean_journal(tmp_path, monkeypatch):
    new_files = {topic: tmp_path / f"{topic}.jsonl" for topic in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", new_files)
    yield


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------

def test_lcid_incident_is_rejected():
    # The exact production numbers: $175.50 credit, $3.50 strike, delta -0.12
    ok, why = csp_credit_is_sane(175.5, 3.5, delta=-0.1227)
    assert not ok
    assert "implausible" in why or "self-contradicting" in why


def test_bb_real_quote_passes_credit_gate():
    # RECLASSIFIED after the review's live audit: BB's $196 quote was REAL
    # (BS at its own IV reprices to $195.56; the 12P trades ~$3 today). At
    # 16% of strike it passes the recalibrated 20% cap — so BB re-enters the
    # record as an honest rule-breaking LOSER (ITM sale, delta -0.53), which
    # the new ITM/delta guard in _pick_best_put now prevents at generation.
    ok, _ = csp_credit_is_sane(196.0, 12.0, delta=-0.534)
    assert ok


def test_normal_csp_credits_pass():
    # WEN: $4 credit on $6 strike (0.7%); RIVN: $7 on $10 (0.7%)
    assert csp_credit_is_sane(4.0, 6.0, delta=-0.05)[0]
    assert csp_credit_is_sane(7.0, 10.0, delta=-0.10)[0]
    # Fat-but-legit high-IV premiums (verified vs a BS grid by the review):
    # 30-delta/45-DTE at IV 1.5 prices ~15.7% of strike
    assert csp_credit_is_sane(157.0, 10.0, delta=-0.30)[0]
    # deep-OTM at IV 1.3 prices ~5.3% of strike
    assert csp_credit_is_sane(53.0, 10.0, delta=-0.149)[0]
    # LCID's REAL post-squeeze price today: ~$24 on the 3.5 strike, delta
    # ~-0.10 (7% of strike) — the exact false positive the audit caught live
    assert csp_credit_is_sane(24.0, 3.5, delta=-0.10)[0]


def test_deep_otm_contradiction_rejected_even_under_main_cap():
    # 9% of strike is fine at 30-delta but garbage at 5-delta (cap 8%)
    assert csp_credit_is_sane(90.0, 10.0, delta=-0.30)[0]
    assert not csp_credit_is_sane(90.0, 10.0, delta=-0.05)[0]


def test_non_positive_credit_or_strike_rejected():
    assert not csp_credit_is_sane(0.0, 10.0)[0]
    assert not csp_credit_is_sane(-5.0, 10.0)[0]
    assert not csp_credit_is_sane(5.0, 0.0)[0]


def test_spread_credit_bounds():
    # $2-wide spread: max legit credit is $200; 90% cap = $180
    assert spread_credit_is_sane(58.0, 24.0, 22.0)[0]      # DKNG-style: fine
    assert not spread_credit_is_sane(190.0, 24.0, 22.0)[0]  # near-width: garbage
    assert not spread_credit_is_sane(50.0, 22.0, 24.0)[0]   # inverted strikes


# ---------------------------------------------------------------------------
# Read-path quarantine
# ---------------------------------------------------------------------------

def _open_event(ticker, strike, credit, delta=None, **extra):
    exp = (datetime.now() + timedelta(days=40)).date().isoformat()
    return {
        "event": "open",
        "trade_id": f"t::{ticker}::{strike}::{exp}",
        "ticker": ticker, "strike": strike, "expiration": exp,
        "opened_at": datetime.now().isoformat(),
        "spot_at_open": strike * 1.4, "dte_at_open": 40,
        "credit_received": credit, "max_loss": strike * 100 - credit,
        "breakeven": strike - credit / 100, "delta_at_open": delta,
        **extra,
    }


def test_open_event_predicate_matches_incident():
    assert not open_event_is_sane(_open_event("LCID", 3.5, 175.5, -0.1227))
    assert open_event_is_sane(_open_event("WEN", 6.0, 4.0, -0.05))


def test_tracker_quarantines_garbage_opens():
    journal.append("virtual_trades", _open_event("LCID", 3.5, 175.5, -0.1227))
    journal.append("virtual_trades", _open_event("WEN", 6.0, 4.0, -0.05))
    open_trades = virtual_tracker.get_open_virtual_trades()
    assert [t.ticker for t in open_trades] == ["WEN"]


def test_evaluator_excludes_garbage_trades_from_stats():
    bad = _open_event("LCID", 3.5, 175.5, -0.1227)
    journal.append("virtual_trades", bad)
    journal.append("virtual_trades", {
        "event": "close", "trade_id": bad["trade_id"], "ticker": "LCID",
        "closed_at": datetime.now().isoformat(),
        "exit_reason": "take_profit_50pct", "pnl": 77.26,
        "strike": 3.5, "expiration": bad["expiration"],
    })
    good = _open_event("WEN", 6.0, 4.0, -0.05)
    journal.append("virtual_trades", good)
    journal.append("virtual_trades", {
        "event": "close", "trade_id": good["trade_id"], "ticker": "WEN",
        "closed_at": datetime.now().isoformat(),
        "exit_reason": "take_profit_50pct", "pnl": 2.0,
        "strike": 6.0, "expiration": good["expiration"],
    })
    closed = evaluator._reconstruct_closed_trades()
    assert [t["ticker"] for t in closed] == ["WEN"]
    summary = evaluator.all_periods_summary()["all"]
    assert summary.closed_count == 1
    assert summary.total_pnl == pytest.approx(2.0)
