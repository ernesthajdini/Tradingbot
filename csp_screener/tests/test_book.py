"""
Position book — replay, sequence P&L across rolls, and the guards.

The reference case is the owner's real Aug-Sep 2026 NVDA chain, which must
score as ONE sequence worth +$1,028.92, not as a loss followed by a win.
"""
from datetime import datetime, timedelta

import pytest

from csp_screener import book, journal


@pytest.fixture(autouse=True)
def tmp_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILES",
                        {t: tmp_path / f"{t}.jsonl" for t in journal.JOURNAL_FILES})
    yield


# ---------------------------------------------------------------------------
# arithmetic
# ---------------------------------------------------------------------------

def test_leg_pnl_charges_both_sides():
    assert book.leg_pnl(1.41, 0.13, 4) == pytest.approx((1.41 - 0.13) * 400 - 8)


def test_expired_worthless_charges_one_side_only():
    assert book.leg_pnl(1.41, 0.0, 4, expired_worthless=True) == \
        pytest.approx(1.41 * 400 - 4)


# ---------------------------------------------------------------------------
# the reference sequence — the owner's real trades
# ---------------------------------------------------------------------------

def test_roll_chain_scores_as_one_sequence():
    """21 Aug: 217.5P bought back at 2.23 (a loss), rolled to 28AUG 210P at
    4.02; 27 Aug: closed at 0.26. One sequence, net positive."""
    book.record_open("NVDA", 217.5, "2026-08-21", 1.07, 4)
    sid = book.seq_id("NVDA", "2026-08-21", 217.5)
    book.record_roll(sid, buyback=2.23, to_strike=210,
                     to_expiration="2026-08-28", credit=4.02)
    book.record_close(sid, buyback=0.26)

    s = book.replay()[sid]
    assert s.status == "closed" and len(s.legs) == 2 and s.rolls == 1
    leg1 = (1.07 - 2.23) * 400 - 8          # -472
    leg2 = (4.02 - 0.26) * 400 - 8          # +1496
    assert s.legs[0].pnl == pytest.approx(leg1)
    assert s.legs[1].pnl == pytest.approx(leg2)
    assert s.realised == pytest.approx(leg1 + leg2)
    assert s.realised > 0                    # the sequence is a winner
    assert s.legs[0].pnl < 0                 # but its first leg was not


def test_open_sequence_carries_realised_loss_into_the_next_leg():
    book.record_open("NVDA", 217.5, "2026-08-21", 1.07, 4)
    sid = book.seq_id("NVDA", "2026-08-21", 217.5)
    book.record_roll(sid, 2.23, 210, "2026-08-28", 4.02)
    s = book.replay()[sid]
    assert s.status == "open"
    assert s.realised == pytest.approx(-472.0)   # locked in, not erased
    assert s.current.strike == 210
    assert s.exposure == 210 * 4 * 100


def test_status_states_the_breakeven_the_new_leg_must_beat():
    book.record_open("NVDA", 217.5, "2026-08-21", 1.07, 4)
    sid = book.seq_id("NVDA", "2026-08-21", 217.5)
    book.record_roll(sid, 2.23, 210, "2026-08-28", 4.02)
    text = book.format_status()
    assert "must beat $472.00" in text


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def test_one_open_sequence_per_ticker():
    book.record_open("NVDA", 215, "2026-10-01", 1.24, 4)
    with pytest.raises(ValueError, match="already has an open sequence"):
        book.record_open("NVDA", 210, "2026-10-08", 1.10, 4)


def test_duplicate_sequence_refused():
    book.record_open("NVDA", 215, "2026-10-01", 1.24, 4)
    sid = book.seq_id("NVDA", "2026-10-01", 215)
    book.record_close(sid, buyback=0.10)
    with pytest.raises(ValueError, match="already exists"):
        book.record_open("NVDA", 215, "2026-10-01", 1.24, 4)


def test_cannot_roll_or_close_a_closed_sequence():
    book.record_open("NVDA", 215, "2026-10-01", 1.24, 4)
    sid = book.seq_id("NVDA", "2026-10-01", 215)
    book.record_close(sid, expired_worthless=True)
    with pytest.raises(ValueError, match="no open sequence"):
        book.record_roll(sid, 1.0, 210, "2026-10-08", 1.5)
    with pytest.raises(ValueError, match="no open sequence"):
        book.record_close(sid, 0.5)


def test_has_open_and_reopen_cooldown():
    book.record_open("NVDA", 215, "2026-10-01", 1.24, 4)
    assert book.has_open("NVDA") and book.has_open("nvda")
    sid = book.seq_id("NVDA", "2026-10-01", 215)
    book.record_close(sid, expired_worthless=True)
    assert not book.has_open("NVDA")
    assert book.in_cooldown("NVDA") is not None      # closed just now
    assert book.in_cooldown("NVDA", days=0) is None
    assert book.in_cooldown("AAPL") is None


def test_journal_is_append_only_and_separate_from_virtual_trades():
    book.record_open("NVDA", 215, "2026-10-01", 1.24, 4)
    sid = book.seq_id("NVDA", "2026-10-01", 215)
    book.record_roll(sid, 2.0, 210, "2026-10-08", 3.0)
    book.record_close(sid, 0.2)
    evs = journal.read_all(book.TOPIC)
    assert [e["event"] for e in evs] == ["open", "roll", "close"]
    assert journal.read_all("virtual_trades") == []   # isolation intact


def test_history_shows_every_leg():
    book.record_open("NVDA", 217.5, "2026-08-21", 1.07, 4)
    sid = book.seq_id("NVDA", "2026-08-21", 217.5)
    book.record_roll(sid, 2.23, 210, "2026-08-28", 4.02)
    book.record_close(sid, 0.26)
    text = book.format_history()
    assert "leg 1" in text and "leg 2" in text and "SEQUENCE P&L" in text
    assert "bought back $2.23" in text
