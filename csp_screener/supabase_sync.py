"""
Supabase dual-write integration.

When SUPABASE_URL and SUPABASE_SERVICE_KEY are set, every journal append
ALSO writes to Supabase. Idempotent via unique (event, trade_id) constraint
for virtual_trades and unique screen_id for screens.

This is opt-in: if the env vars aren't set, this module is a no-op. The
local JSONL journal continues to work as before.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_client = None
_initialized = False


def _get_client():
    """Lazy-load Supabase client. Returns None if not configured."""
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        logger.info("Supabase not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY); local-only mode")
        return None

    try:
        from supabase import create_client
        _client = create_client(url, key)
        logger.info(f"Supabase client initialized for {url}")
    except ImportError:
        logger.warning("supabase-py not installed; pip install supabase")
        _client = None
    except Exception as e:
        logger.warning(f"Failed to initialize Supabase client: {e}")
        _client = None
    return _client


def is_enabled() -> bool:
    return _get_client() is not None


# ---------------------------------------------------------------------------
# Topic-specific upserts
# ---------------------------------------------------------------------------

def push_screen(record: dict) -> bool:
    """
    Push a screen record to Supabase. Idempotent on screen_id.
    Returns True if written, False otherwise.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        row = {
            "screen_id": record.get("screen_id"),
            "run_type": record.get("run_type") or "weekly",
            "ran_at": record.get("ran_at"),
            "universe_size": record.get("universe_size"),
            "passed_filters": record.get("passed_filters"),
            "candidates_in_email": record.get("candidates_in_email"),
            "virtual_positions_opened": record.get("virtual_positions_opened"),
            "virtual_closed_this_run": record.get("virtual_closed_this_run"),
            "virtual_closed_pnl": record.get("virtual_closed_pnl"),
            "vix": record.get("vix"),
            "email_sent": record.get("email_sent", False),
            "tickers_in_email": record.get("tickers_in_email") or [],
            # We omit very large payloads from the main row and put them in JSONB
            "candidates_payload": record.get("candidates_payload"),
            "summaries_payload": record.get("summaries"),
            "recommendations_payload": record.get("recommendations"),
            "live_viable": record.get("live_viable"),
            "no_trade_week": record.get("no_trade_week"),
            "post_spike_window": record.get("post_spike_window"),
            "fomc_days": record.get("fomc_days"),
            "eur_usd_rate": record.get("eur_usd_rate"),
            "record_hash": record.get("record_hash"),
            "recorded_at": record.get("recorded_at"),
        }
        # Use upsert on screen_id (the natural key) to be idempotent
        client.table("screens").upsert(row, on_conflict="screen_id").execute()
        return True
    except Exception as e:
        logger.warning(f"Supabase push_screen failed: {e}")
        return False


def push_virtual_trade(record: dict) -> bool:
    """
    Push a virtual_trades event (open or close) to Supabase.
    Idempotent on (event, trade_id).
    """
    client = _get_client()
    if client is None:
        return False
    try:
        # Build row from event-specific fields, leaving NULLs where N/A
        row = {
            "event": record.get("event"),
            "trade_id": record.get("trade_id"),
            "screen_id": record.get("screen_id"),
            "ticker": record.get("ticker"),
            "strike": record.get("strike"),
            "expiration": record.get("expiration"),
            "opened_at": record.get("opened_at"),
            "spot_at_open": record.get("spot_at_open"),
            "dte_at_open": record.get("dte_at_open"),
            "credit_received": record.get("credit_received"),
            "max_loss": record.get("max_loss"),
            "breakeven": record.get("breakeven"),
            "delta_at_open": record.get("delta_at_open"),
            "iv_at_open": record.get("iv_at_open"),
            "data_quality": record.get("data_quality"),
            "closed_at": record.get("closed_at"),
            "exit_reason": record.get("exit_reason"),
            "exit_spot": record.get("exit_spot"),
            "final_put_price": record.get("final_put_price"),
            "pnl": record.get("pnl"),
            "pnl_gross": record.get("pnl_gross"),
            "friction": record.get("friction"),
            "pnl_pessimistic": record.get("pnl_pessimistic"),
            "eur_usd_rate": record.get("eur_usd_rate"),
            "pnl_eur": record.get("pnl_eur"),
            "structure": record.get("structure"),
            "tier": record.get("tier"),
            "long_strike": record.get("long_strike"),
            "pnl_pct_of_credit": record.get("pnl_pct_of_credit"),
            "notes": record.get("notes"),
            "record_hash": record.get("record_hash"),
            "recorded_at": record.get("recorded_at"),
        }
        # Strip Nones to keep payload small
        row = {k: v for k, v in row.items() if v is not None}
        client.table("virtual_trades").upsert(
            row, on_conflict="event,trade_id"
        ).execute()
        return True
    except Exception as e:
        logger.warning(f"Supabase push_virtual_trade failed: {e}")
        return False


def push_system_event(record: dict) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        row = {
            "event": record.get("event"),
            "at": record.get("at") or record.get("recorded_at"),
            "payload": record,
            "recorded_at": record.get("recorded_at"),
        }
        client.table("system_events").insert(row).execute()
        return True
    except Exception as e:
        logger.warning(f"Supabase push_system_event failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Topic dispatch (called by journal.append)
# ---------------------------------------------------------------------------

def push(topic: str, record: dict) -> bool:
    """Dispatch by topic name. Returns True if pushed."""
    if topic == "screens":
        return push_screen(record)
    if topic == "virtual_trades":
        return push_virtual_trade(record)
    if topic == "system_events":
        return push_system_event(record)
    if topic == "evaluations":
        # Evaluations are computed on-read for the dashboard; don't store
        return False
    logger.debug(f"Unknown topic for Supabase push: {topic}")
    return False


# ---------------------------------------------------------------------------
# Hydration — pull cloud state DOWN into the local JSONL journal
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: GitHub Actions runners are stateless. Every cloud run
# starts with an empty local journal, but the virtual tracker reconstructs
# open positions by replaying the local journal. Without hydration, cloud
# runs would see zero open positions, never close anything, and the learning
# layer would never learn. Supabase is the durable store; this pulls it back.
#
# Merge semantics: union keyed by (event, trade_id). Local records win (they
# may not have been pushed yet); cloud records missing locally are appended.
# Timestamps are normalized to tz-naive ISO so downstream datetime math
# (which uses naive datetime.now()) never hits aware-vs-naive TypeErrors.

_HYDRATE_FIELDS_FLOAT = (
    "strike", "spot_at_open", "credit_received", "max_loss", "breakeven",
    "delta_at_open", "iv_at_open", "exit_spot", "final_put_price",
    "pnl", "pnl_gross", "friction", "pnl_pessimistic", "eur_usd_rate",
    "pnl_eur", "long_strike", "pnl_pct_of_credit",
)
_HYDRATE_FIELDS_TS = ("opened_at", "closed_at", "recorded_at")


def _naive_iso(value) -> Optional[str]:
    """Normalize any ISO-ish timestamp to tz-naive ISO string."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None).isoformat()
    except (ValueError, TypeError):
        return str(value)


def _row_to_journal_record(row: dict) -> dict:
    """Convert a Supabase virtual_trades row back into a journal record."""
    rec = {}
    for k in (
        "event", "trade_id", "screen_id", "ticker", "expiration",
        "dte_at_open", "data_quality", "exit_reason", "notes", "record_hash",
        "structure", "tier",
    ):
        if row.get(k) is not None:
            rec[k] = row[k]
    for k in _HYDRATE_FIELDS_FLOAT:
        if row.get(k) is not None:
            try:
                rec[k] = float(row[k])
            except (ValueError, TypeError):
                pass
    for k in _HYDRATE_FIELDS_TS:
        norm = _naive_iso(row.get(k))
        if norm:
            rec[k] = norm
    if rec.get("dte_at_open") is not None:
        try:
            rec["dte_at_open"] = int(rec["dte_at_open"])
        except (ValueError, TypeError):
            pass
    return rec


def hydrate_virtual_trades() -> dict:
    """
    Merge cloud virtual_trades into the local journal (union by event+trade_id).
    Safe to call every run; no-op when Supabase isn't configured or nothing
    is missing locally. Writes the JSONL file directly (NOT via journal.append)
    so hydrated records don't get re-pushed to the cloud.
    """
    client = _get_client()
    if client is None:
        return {"hydrated": False, "reason": "supabase not configured"}

    import json as _json
    from csp_screener import journal

    try:
        local = journal.read_all("virtual_trades")
        local_keys = {(r.get("event"), r.get("trade_id")) for r in local}

        rows: list[dict] = []
        page_size = 1000
        offset = 0
        while True:
            res = (
                client.table("virtual_trades")
                .select("*")
                .order("id")
                .range(offset, offset + page_size - 1)
                .execute()
            )
            batch = res.data or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        added = 0
        path = journal.JOURNAL_FILES["virtual_trades"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for row in rows:
                key = (row.get("event"), row.get("trade_id"))
                if key in local_keys:
                    continue
                rec = _row_to_journal_record(row)
                if not rec.get("event") or not rec.get("trade_id"):
                    continue
                f.write(_json.dumps(rec, separators=(",", ":")) + "\n")
                local_keys.add(key)
                added += 1

        if added:
            logger.info(f"Hydrated {added} virtual_trades records from Supabase")
        return {"hydrated": True, "added": added, "cloud_rows": len(rows)}
    except Exception as e:
        logger.warning(f"Hydration from Supabase failed: {e}")
        return {"hydrated": False, "reason": str(e)}


# ---------------------------------------------------------------------------
# CI verification — turn silent push failures into loud red workflow runs
# ---------------------------------------------------------------------------

def verify_recent_screen(max_age_hours: int = 6) -> bool:
    """
    True if Supabase has a screens row recorded within max_age_hours.
    Used as a post-run CI gate: pushes are individually non-fatal (a DB blip
    shouldn't kill a screen), but a run that wrote NOTHING to the cloud must
    fail loudly instead of showing a lying green checkmark.

    Returns True when Supabase isn't configured (nothing to verify).
    """
    client = _get_client()
    if client is None:
        return True
    try:
        res = (
            client.table("screens")
            .select("screen_id,recorded_at")
            .order("recorded_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            logger.error("verify_recent_screen: screens table is EMPTY")
            return False
        ts = datetime.fromisoformat(
            str(rows[0]["recorded_at"]).replace("Z", "+00:00")
        )
        from datetime import timedelta, timezone
        age = datetime.now(timezone.utc) - ts
        ok = age < timedelta(hours=max_age_hours)
        if not ok:
            logger.error(
                f"verify_recent_screen: newest screen is {age} old "
                f"(> {max_age_hours}h) — this run's push did not land"
            )
        return ok
    except Exception as e:
        logger.error(f"verify_recent_screen failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Backfill helper — push everything from local JSONL into Supabase
# ---------------------------------------------------------------------------

def backfill_all_topics() -> dict:
    """
    One-shot: read every JSONL journal and push every record to Supabase.
    Safe to run multiple times — upsert keys prevent duplicates.

    Use this when you first set up Supabase and want to migrate local history.
    """
    from csp_screener import journal
    summary = {"screens": 0, "virtual_trades": 0, "system_events": 0}
    for topic in summary.keys():
        records = journal.read_all(topic)
        ok = 0
        for r in records:
            if push(topic, r):
                ok += 1
        summary[topic] = ok
        logger.info(f"Backfilled {ok}/{len(records)} records to {topic}")
    return summary
