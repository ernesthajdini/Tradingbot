"""
FULL-STUDY PHASE 1 — stock EOD sweep over every option root, 2015→present.

Purpose: as-of-date universe reconstruction (MANIFEST survivorship rail).
For every symbol that has ever had listed options, pull its full as-traded
stock EOD history (Stocks STANDARD: 2016-01-01+; we request from 2015-01-01
and take what's served — the pre-2016 tail, where available, feeds the
252-day RV warmup for the first study year).

Output: backtest/data/thetadata_full/stocks/<SYMBOL>.csv (as served).
Symbols with no stock data (indices, some OTC) get an empty .none marker so
resume skips them. Progress: one line per 100 symbols to sweep.log.

Concurrency 4 (the account's server-side allowance — exceeding it only
queues). Resumable: re-run skips complete files.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = "http://127.0.0.1:25503"
OUT = Path(__file__).resolve().parent / "data" / "thetadata_full" / "stocks"
# Stocks STANDARD serves 2016-01-01+ and REJECTS (403, tier wall) any
# request that reaches earlier — it does not clip. 2015 warmup is therefore
# unavailable: the 2016 study year runs with a shortened RV-percentile
# lookback (the ranker degrades gracefully), stamped in the study notes.
START = "2016-01-01"
END = "2026-08-25"
WORKERS = 4
RETRIES = [0, 5, 20, 60]


def all_option_symbols() -> list[str]:
    for backoff in RETRIES:
        if backoff:
            time.sleep(backoff)
        try:
            r = requests.get(f"{BASE}/v3/option/list/symbols", timeout=120)
        except requests.RequestException:
            continue
        syms = []
        for line in r.text.splitlines()[1:]:
            s = line.strip().strip('"')
            if not s:
                continue
            # Digit-prefixed roots (1BXSL, 2CLSK, 9AC...) are corporate-
            # action adjusted series — their primary roots are listed
            # separately and carry the stock history. Skip the aliases.
            if s[0].isdigit():
                continue
            # Reserved DOS device names (a ticker literally named AUX
            # exists) cannot be used as Windows filenames — Remove-Item
            # chokes on them. Skip; they are not band candidates anyway.
            if s.upper() in {"CON", "PRN", "AUX", "NUL"} or (
                    len(s) == 4 and s[:3].upper() in {"COM", "LPT"}
                    and s[3].isdigit()):
                continue
            syms.append(s)
        # A momentarily-empty or error response must NEVER become a silent
        # "0 symbols, sweep done" success (it did, once).
        if len(syms) > 1000:
            return sorted(set(syms))
    raise RuntimeError("option symbol listing returned an implausibly small "
                       "set after retries — is the terminal healthy?")


def _fetch_year(sym: str, y0: str, y1: str) -> str | None:
    """One ≤365-day chunk (the API rejects longer ranges). Returns CSV text,
    '' for no-data, or None on hard failure."""
    for backoff in RETRIES:
        if backoff:
            time.sleep(backoff)
        try:
            r = requests.get(f"{BASE}/v3/stock/history/eod", params={
                "symbol": sym, "start_date": y0, "end_date": y1,
            }, timeout=180)
        except requests.RequestException:
            continue
        text = r.text
        if r.status_code == 200 and text.startswith("created"):
            return text
        low = text[:200].lower()
        # A tier wall is a CONFIGURATION error, never a per-symbol fact —
        # marking it .none would poison every resume (it did, once: a 2015
        # start date 403'd all 15k symbols in 40 seconds as 'none').
        if "subscription" in low:
            raise RuntimeError(f"TIER WALL on {sym}: {text[:160]}")
        if "no data" in low or "not found" in low or "invalid" in low:
            return ""
        if r.status_code in (429, 500, 502, 503, 504):
            continue
        return ""
    return None


def fetch_one(sym: str) -> str:
    out = OUT / f"{sym}.csv"
    marker = OUT / f"{sym}.none"
    if (out.exists() and out.stat().st_size > 200) or marker.exists():
        return "skip"
    # Year-by-year chunks ("max 365 days allowed" per request), concatenated
    # with a single header. A symbol is .none only when EVERY year is empty.
    parts: list[str] = []
    y = int(START[:4])
    end_year = int(END[:4])
    failed = False
    while y <= end_year:
        y0 = f"{y}-01-01" if f"{y}-01-01" > START else START
        y1 = f"{y}-12-31" if f"{y}-12-31" < END else END
        text = _fetch_year(sym, y0, y1)
        if text is None:
            failed = True
        elif text:
            lines = text.splitlines()
            parts.append("\n".join(lines if not parts else lines[1:]))
        y += 1
    if failed and not parts:
        return "fail"
    if not parts:
        marker.touch()
        return "empty"
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return "ok"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    syms = all_option_symbols()
    log = open(OUT.parent / "sweep.log", "a", encoding="utf-8", buffering=1)
    print(f"{len(syms)} symbols to sweep", file=log)
    counts = {"ok": 0, "skip": 0, "none": 0, "empty": 0, "fail": 0}
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        futures = {ex.submit(fetch_one, s): s for s in syms}
        done = 0
        for fut in as_completed(futures):
            counts[fut.result()] = counts.get(fut.result(), 0) + 1
            done += 1
            if done % 100 == 0:
                rate = done / (time.time() - t0)
                print(f"{done}/{len(syms)} ({rate:.1f}/s) {counts}", file=log)
    print(f"SWEEP DONE in {(time.time()-t0)/60:.0f}min: {counts}", file=log)
    print(f"SWEEP DONE: {counts}")
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
