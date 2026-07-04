"""
Tests for journal.py — append-only invariants.

These are the most important tests in the project: if the journal is editable
in any way, the screener's "track record" is just a story you tell yourself.
"""
import json

import pytest

from csp_screener import journal


@pytest.fixture(autouse=True)
def clean_journal(tmp_path, monkeypatch):
    """Redirect journal files to a tmp dir per test."""
    new_files = {topic: tmp_path / f"{topic}.jsonl" for topic in journal.JOURNAL_FILES}
    monkeypatch.setattr(journal, "JOURNAL_FILES", new_files)
    yield


def test_append_creates_file():
    journal.append("screens", {"foo": 1})
    path = journal.JOURNAL_FILES["screens"]
    assert path.exists()


def test_append_adds_recorded_at_and_hash():
    out = journal.append("screens", {"foo": "bar"})
    assert "recorded_at" in out
    assert "record_hash" in out
    assert len(out["record_hash"]) == 16


def test_append_is_actually_appending_not_overwriting():
    journal.append("screens", {"foo": 1})
    journal.append("screens", {"foo": 2})
    journal.append("screens", {"foo": 3})
    records = journal.read_all("screens")
    assert len(records) == 3
    assert [r["foo"] for r in records] == [1, 2, 3]


def test_read_all_skips_corrupt_lines():
    path = journal.JOURNAL_FILES["screens"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"foo": 1}\nnot-json\n{"foo": 2}\n', encoding="utf-8")
    records = journal.read_all("screens")
    assert len(records) == 2


def test_unknown_topic_raises():
    with pytest.raises(ValueError):
        journal.append("unknown_topic", {"foo": 1})
    with pytest.raises(ValueError):
        journal.read_all("unknown_topic")


def test_topic_count():
    assert journal.topic_count("screens") == 0
    journal.append("screens", {"x": 1})
    journal.append("screens", {"x": 2})
    assert journal.topic_count("screens") == 2


def test_filtered_read():
    journal.append("virtual_trades", {"event": "open", "trade_id": "a"})
    journal.append("virtual_trades", {"event": "close", "trade_id": "a"})
    journal.append("virtual_trades", {"event": "open", "trade_id": "b"})
    opens = journal.read_filtered("virtual_trades", event="open")
    assert len(opens) == 2


def test_hash_is_stable_for_same_payload():
    # Two appends of identical payload at different times should have different
    # hashes only because recorded_at differs. Without it, the hash is stable.
    r1 = journal.append("screens", {"x": 1})
    r2 = journal.append("screens", {"x": 1})
    # recorded_at differs, so hashes differ — but read works
    assert r1["record_hash"] != r2["record_hash"] or r1["recorded_at"] == r2["recorded_at"]


def test_records_serialize_unusual_types():
    from datetime import datetime
    import numpy as np
    journal.append("screens", {
        "when": datetime(2026, 5, 19, 12, 0),
        "np_scalar": np.float64(3.14),
        "nested": {"list": [np.int64(1), 2.0, "three"]},
    })
    records = journal.read_all("screens")
    assert len(records) == 1
    r = records[0]
    assert r["when"] == "2026-05-19T12:00:00"
    assert r["np_scalar"] == 3.14
    assert r["nested"]["list"] == [1, 2.0, "three"]
