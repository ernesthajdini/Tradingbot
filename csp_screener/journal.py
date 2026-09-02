"""
Append-only JSONL journal.

DESIGN: every write is an append. There is no update path. There is no delete
path. Records are never modified after they're written.

Why: a journal you can rewrite is a journal you'll rewrite, and then you can't
trust your own track record. Append-only is enforced at the API level — no
helper exists for any other operation.

State (e.g., "is virtual trade X currently open?") is computed by REPLAYING
the event log. To "close" a virtual trade, you append a close event with the
same trade_id. The reader sees both events and knows the trade is closed.

This pattern is borrowed from event-sourced systems and prevents the most
common journal-corruption mode: post-hoc rewriting after a bad outcome.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from csp_screener import config

logger = logging.getLogger(__name__)

# All journal files live here, organized by topic.
# shadow_trades is a SEPARATE topic by design: golive.gate_status() and the
# evaluator pool every close in virtual_trades, so counterfactual (shadow)
# trades must live where the gate can never read them.
JOURNAL_FILES = {
    "screens": config.JOURNAL_DIR / "screens.jsonl",
    "virtual_trades": config.JOURNAL_DIR / "virtual_trades.jsonl",
    "shadow_trades": config.JOURNAL_DIR / "shadow_trades.jsonl",
    "evaluations": config.JOURNAL_DIR / "evaluations.jsonl",
    "system_events": config.JOURNAL_DIR / "system_events.jsonl",
    # paper_orders: what the IBKR PAPER account did with each paper trade.
    # Separate topic on purpose — the go-live gate and the evaluator only
    # read virtual_trades, so broker fills can never alter the record.
    "paper_orders": config.JOURNAL_DIR / "paper_orders.jsonl",
}


def _serialize(obj):
    """Make dataclasses + datetimes JSON-safe."""
    if is_dataclass(obj):
        return _serialize(asdict(obj))
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if hasattr(obj, "item"):  # numpy scalar
        try:
            return obj.item()
        except Exception:
            return str(obj)
    return obj


def append(topic: str, record: dict) -> dict:
    """
    Append a record to the named topic. Records get auto-stamped fields:
      - 'recorded_at': UTC-naive timestamp of the write
      - 'record_hash': sha256 of the serialized payload (tamper detection)

    Returns the final record (with stamps) for caller logging.
    """
    if topic not in JOURNAL_FILES:
        raise ValueError(f"Unknown journal topic: {topic}. Known: {list(JOURNAL_FILES)}")
    path = JOURNAL_FILES[topic]
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = _serialize(record)
    payload.setdefault("recorded_at", datetime.now().isoformat())

    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["record_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    # Atomic append: write to a temp file, then rename? No — for jsonl, simple
    # text append with explicit fsync is fine and faster.
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":")) + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # Windows fsync sometimes restricted; not fatal

    # Dual-write to Supabase if configured. Local write is the source of truth;
    # Supabase failures don't raise — they just get logged.
    try:
        from csp_screener import supabase_sync
        if supabase_sync.is_enabled():
            supabase_sync.push(topic, payload)
    except Exception as e:
        logger.debug(f"Supabase dual-write failed (non-fatal): {e}")

    return payload


def read_all(topic: str) -> list[dict]:
    """Read every record from a topic. Returns empty list if file missing."""
    if topic not in JOURNAL_FILES:
        raise ValueError(f"Unknown journal topic: {topic}")
    path = JOURNAL_FILES[topic]
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"Skipping corrupt journal line in {path.name}: {e}")
    return out


def read_filtered(topic: str, **filters) -> list[dict]:
    """Read records and filter by exact-match field values."""
    records = read_all(topic)
    if not filters:
        return records
    return [
        r for r in records
        if all(r.get(k) == v for k, v in filters.items())
    ]


def topic_count(topic: str) -> int:
    """Number of records in a topic file (line count, excluding blanks)."""
    if topic not in JOURNAL_FILES:
        raise ValueError(f"Unknown journal topic: {topic}")
    path = JOURNAL_FILES[topic]
    if not path.exists():
        return 0
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count
