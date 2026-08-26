"""
FULL-STUDY PHASE 3d — targeted options pull for precomputed candidates.

Reads candidates.json (the exact daily top-N per tier the production
pipeline would have ranked, 2016→2026) and pulls put EOD + OI ONLY for the
(ticker, expiration) pairs those candidate-days can reach: expirations
within [day+DTE_MIN, day+DTE_MAX], each over its full active window
[exp-70d, exp] so entry AND marking to exit are covered.

Same layout and resume semantics as options_pull_full.py (which this
replaces — the blind pull measured ~38 days; this is the same evidence in
~a day). Files already pulled are reused.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from csp_screener import config  # noqa: E402

BASE = "http://127.0.0.1:25503"
DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
OUT = DATA / "options"
EXPIRY_ACTIVE_DAYS = 70
WORKERS = 4
RETRIES = [0, 5, 20, 60]
DATA_START = date(2016, 1, 4)
DATA_END = date(2026, 8, 25)


def _get(path: str, params: dict, sym: str) -> str | None:
    for backoff in RETRIES:
        if backoff:
            time.sleep(backoff)
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=300)
        except requests.RequestException:
            continue
        text = r.text
        low = text[:160].lower()
        if "subscription" in low:
            raise RuntimeError(f"TIER WALL on {sym}: {text[:160]}")
        if r.status_code == 200 and (low.startswith("symbol")
                                     or low.startswith("created")):
            return text
        if r.status_code in (429, 500, 502, 503, 504):
            continue
        return ""
    return None


def pull_ticker(sym: str, days: list[date]) -> dict:
    tdir = OUT / sym
    tdir.mkdir(parents=True, exist_ok=True)
    counts = {"puts": 0, "oi": 0, "skip": 0, "empty": 0, "fail": 0}

    text = _get("/v3/option/list/expirations", {"symbol": sym}, sym)
    if not text:
        return counts
    exps = set()
    for line in text.splitlines()[1:]:
        parts = line.replace('"', "").split(",")
        if len(parts) >= 2:
            try:
                exps.add(date.fromisoformat(parts[1]))
            except ValueError:
                pass

    needed: set[date] = set()
    for d in days:
        for exp in exps:
            if d + timedelta(days=config.DTE_MIN) <= exp \
                    <= d + timedelta(days=config.DTE_MAX):
                needed.add(exp)

    for exp in sorted(needed):
        lo = max(exp - timedelta(days=EXPIRY_ACTIVE_DAYS), DATA_START)
        hi = min(exp, DATA_END)
        if lo > hi:
            continue
        for kind, path in (("puts", "/v3/option/history/eod"),
                           ("oi", "/v3/option/history/open_interest")):
            out = tdir / f"{kind}_{exp.isoformat()}.csv"
            if out.exists():
                counts["skip"] += 1
                continue
            text = _get(path, {
                "symbol": sym, "expiration": exp.isoformat(), "right": "put",
                "start_date": lo.isoformat(), "end_date": hi.isoformat(),
            }, sym)
            if text is None:
                counts["fail"] += 1
            elif text and len(text.splitlines()) > 1:
                out.write_text(text, encoding="utf-8")
                counts[kind] += 1
            else:
                out.write_text("", encoding="utf-8")
                counts["empty"] += 1
    return counts


def main() -> int:
    cand = json.loads((DATA / "candidates.json").read_text(encoding="utf-8"))
    demand: dict[str, list[date]] = {}
    for day, tiers in cand["days"].items():
        d = date.fromisoformat(day)
        for syms in tiers.values():
            for s in syms:
                demand.setdefault(s, []).append(d)
    log = open(DATA / "ranked_pull.log", "a", encoding="utf-8", buffering=1)
    print(f"{len(demand)} candidate tickers "
          f"({sum(len(v) for v in demand.values()):,} candidate-days)",
          file=log)
    totals = {"puts": 0, "oi": 0, "skip": 0, "empty": 0, "fail": 0}
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futures = {ex.submit(pull_ticker, s, ds): s for s, ds in demand.items()}
        for fut in as_completed(futures):
            c = fut.result()
            for k in totals:
                totals[k] += c.get(k, 0)
            done += 1
            if done % 25 == 0:
                print(f"{done}/{len(demand)} tickers "
                      f"({(time.time()-t0)/3600:.1f}h) {totals}", file=log)
    print(f"RANKED PULL DONE in {(time.time()-t0)/3600:.1f}h: {totals}",
          file=log)
    print(f"RANKED PULL DONE: {totals}")
    return 0 if totals["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
