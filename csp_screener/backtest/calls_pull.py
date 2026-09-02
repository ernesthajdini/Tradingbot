"""
AMENDMENT 12 DATA — single-name CALL EOD pull.

Identical to options_pull_full.py except right=call, calls_<EXP>.csv output,
and its own .complete_calls marker so finished PUT tickers are not skipped.
No OI: the put pull already carries OI, and call OI is not needed to test
short call spreads. Halving the requests is what fits this inside the
remaining paid window.

ORIGINAL HEADER FOLLOWS
-----------------------
FULL-STUDY PHASE 3 — options EOD + OI pull for the as-of universe.

For every ticker in members.json, for every expiration whose active window
([exp - 70d, exp]) intersects a membership interval padded by DTE_MAX+1:
pull put EOD quotes and open interest over that intersection.

Output: data/thetadata_full/options/<TICKER>/puts_<EXP>.csv, oi_<EXP>.csv
Resumable per file. 4 worker threads across tickers (the account's server
allowance). Tier walls raise (config error, never a data fact). Progress to
options_pull.log every 25 tickers.

Scale: ~7k tickers, low-hundreds of thousands of requests — a multi-day
background job. Re-running after any interruption resumes where it stopped.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import requests

BASE = "http://127.0.0.1:25503"
DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
OUT = DATA / "options"
EXPIRY_ACTIVE_DAYS = 70   # entry at <=45 DTE + marking to expiry, with margin
PAD_DAYS = 46             # membership end + open-position tail
WORKERS = 4
RETRIES = [0, 5, 20, 60]
DATA_START = date(2016, 1, 4)
DATA_END = date(2026, 8, 25)
PULL_OI = False  # calls pull never fetches OI


def _get(path: str, params: dict, sym: str) -> str | None:
    """Returns CSV text, '' for no-data, None for hard failure."""
    for backoff in RETRIES:
        if backoff:
            time.sleep(backoff)
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=300)
        except requests.RequestException:
            continue
        text = r.text
        if r.status_code == 200 and not text.startswith("<"):
            low = text[:160].lower()
            if "subscription" in low:
                raise RuntimeError(f"TIER WALL on {sym}: {text[:160]}")
            if low.startswith("symbol") or low.startswith("created"):
                return text
            if "no data" in low or "not found" in low or "invalid" in low:
                return ""
            return ""
        if r.status_code in (429, 500, 502, 503, 504):
            continue
        low = text[:160].lower()
        if "subscription" in low:
            raise RuntimeError(f"TIER WALL on {sym}: {text[:160]}")
        return ""
    return None


def _merge_intervals(iv: list[list[str]]) -> list[tuple[date, date]]:
    spans = sorted((date.fromisoformat(a),
                    date.fromisoformat(b) + timedelta(days=PAD_DAYS))
                   for a, b in iv)
    out: list[list[date]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, min(b, DATA_END)) for a, b in out]


def pull_ticker(sym: str, intervals: list[list[str]]) -> dict:
    tdir = OUT / sym
    tdir.mkdir(parents=True, exist_ok=True)
    done_marker = tdir / ".complete_calls"
    counts = {"calls": 0, "skip": 0, "empty": 0, "fail": 0}
    if done_marker.exists():
        counts["skip"] = 1
        return counts

    spans = _merge_intervals(intervals)
    text = _get("/v3/option/list/expirations", {"symbol": sym}, sym)
    if not text:
        done_marker.touch()
        return counts
    exps = []
    for line in text.splitlines()[1:]:
        parts = line.replace('"', "").split(",")
        if len(parts) >= 2:
            try:
                exps.append(date.fromisoformat(parts[1]))
            except ValueError:
                pass

    for exp in sorted(set(exps)):
        active0 = exp - timedelta(days=EXPIRY_ACTIVE_DAYS)
        for a, b in spans:
            # Membership decides WHETHER to pull this expiration; once it
            # qualifies, pull its FULL active window (same request count,
            # complete data — this is a permanent archive, not a study
            # slice, and a half-window file can never be topped up later
            # because the filename marks the expiration as done).
            if max(active0, a) > min(exp, b):
                continue
            lo, hi = max(active0, DATA_START), min(exp, DATA_END)
            if lo > hi:
                continue
            endpoints = [("calls", "/v3/option/history/eod")]
            for kind, path in endpoints:
                out = tdir / f"{kind}_{exp.isoformat()}.csv"
                if out.exists():
                    counts["skip"] += 1
                    continue
                text = _get(path, {
                    "symbol": sym, "expiration": exp.isoformat(),
                    "right": "call",
                    "start_date": lo.isoformat(), "end_date": hi.isoformat(),
                }, sym)
                if text is None:
                    counts["fail"] += 1
                elif text and len(text.splitlines()) > 1:
                    out.write_text(text, encoding="utf-8")
                    counts[kind] += 1
                else:
                    out.write_text("", encoding="utf-8")  # empty = tried
                    counts["empty"] += 1
            break  # one span per expiration (spans are merged/disjoint)

    if counts["fail"] == 0:
        done_marker.touch()
    return counts


def main() -> int:
    # --no-oi: ARCHIVE MODE for the mop-up of never-ranked names — halves
    # the request count so the full universe fits inside the paid month.
    # Study-critical names get OI via options_pull_ranked.py first; a
    # second OI pass can run if subscription time remains.
    global PULL_OI
    PULL_OI = False   # calls never pull OI, regardless of argv

    members = json.loads((DATA / "members.json").read_text(encoding="utf-8"))
    tickers = members["tickers"]
    # Union of both tiers' intervals per ticker
    work = {}
    for sym, tiers in tickers.items():
        iv = [x for spans in tiers.values() for x in spans]
        if iv:
            work[sym] = iv
    # Most member-days first: if the month ends mid-pull, the names most
    # likely to ever matter are already on disk.
    def _days(iv):
        return sum((date.fromisoformat(b) - date.fromisoformat(a)).days + 1
                   for a, b in iv)
    work = dict(sorted(work.items(), key=lambda kv: -_days(kv[1])))
    log = open(DATA / "calls_pull.log", "a", encoding="utf-8", buffering=1)
    print(f"{len(work)} member tickers to pull (oi={'on' if PULL_OI else 'OFF'})",
          file=log)
    totals = {"calls": 0, "skip": 0, "empty": 0, "fail": 0, "crashed": 0}
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futures = {ex.submit(pull_ticker, s, iv): s for s, iv in work.items()}
        for fut in as_completed(futures):
            # One bad ticker must not kill a multi-day job (the first run of
            # this script died on exactly that). Tier walls stay fatal: they
            # mean the subscription is wrong, and every request after one is
            # wasted quota.
            try:
                c = fut.result()
            except RuntimeError:
                raise                       # TIER WALL — stop everything
            except Exception as e:
                totals["crashed"] += 1
                print(f"CRASH {futures[fut]}: {e!r}", file=log)
                c = {}
            for k in totals:
                totals[k] += c.get(k, 0)
            done += 1
            if done % 25 == 0:
                el = (time.time() - t0) / 3600
                print(f"{done}/{len(work)} tickers ({el:.1f}h) {totals}",
                      file=log)
    print(f"OPTIONS PULL DONE in {(time.time()-t0)/3600:.1f}h: {totals}",
          file=log)
    print(f"OPTIONS PULL DONE: {totals}")
    return 0 if totals["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
