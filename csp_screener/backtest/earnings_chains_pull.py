"""
AMENDMENT 14A DATA — targeted option chains around earnings, liquid names.

The archive was pulled for the 25-45 DTE monthly strategy on the screener's
own candidate windows, so it covers ~27% of earnings events and none of the
liquid large caps where the crush is documented. This pulls exactly what the
earnings study needs and nothing else:

  for each liquid name and each earnings date E (2017-02..2023-12):
    EVENT expiry   = nearest expiry with 7-30 DTE measured at E-1,
                     window [E-12, E+5]
    CONTROL expiry = nearest expiry with 7-30 DTE measured at E-31,
                     window [E-42, E-25]   (the matched mid-quarter arm)
    puts + calls EOD, put + call open interest  -> 8 requests per event

Output: data/thetadata_full/options_earn/<TICKER>/{puts,calls,oi,oi_calls}_<EXP>.csv
Same filenames the existing loaders expect. A file requested twice (two
events sharing an expiry) is MERGED, not skipped. Resumable per ticker.

    python csp_screener/backtest/earnings_chains_pull.py
"""

from __future__ import annotations

import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

from csp_screener.backtest.calls_pull import DATA, WORKERS, _get
from csp_screener.backtest.pattern_study import load_ohlc
from csp_screener.backtest.tail_study import load_earnings

OUT = DATA / "options_earn"
WINDOW = (date(2017, 2, 1), date(2023, 12, 31))
MIN_AVG_VOL = 3_000_000
MIN_PRICE = 10.0
DTE_LO, DTE_HI = 7, 30
EVENT_WIN = (-12, 5)
CONTROL_OFFSET = 31
CONTROL_WIN = (-42, -25)

ENDPOINTS = [("puts", "/v3/option/history/eod", "put"),
             ("calls", "/v3/option/history/eod", "call"),
             ("oi", "/v3/option/history/open_interest", "put"),
             ("oi_calls", "/v3/option/history/open_interest", "call")]


def liquid_names(earn):
    out = []
    for t in sorted(earn):
        p = DATA / "stocks" / f"{t}.csv"
        if not p.exists():
            continue
        try:
            df = load_ohlc(p)
        except Exception:
            continue
        d = df[(df.index >= "2017-01-01") & (df.index <= "2023-12-31")]
        if (len(d) > 500 and d["v"].mean() >= MIN_AVG_VOL
                and d["c"].median() >= MIN_PRICE):
            out.append(t)
    return out


def nearest_expiry(exps, asof):
    cands = [e for e in exps if DTE_LO <= (e - asof).days <= DTE_HI]
    return min(cands, key=lambda e: (e - asof).days) if cands else None


def write_merged(out: Path, text: str):
    new = pd.read_csv(io.StringIO(text))
    if out.exists() and out.stat().st_size > 50:
        try:
            old = pd.read_csv(out)
            new = pd.concat([old, new], ignore_index=True)
        except Exception:
            pass
    key = [c for c in ("created", "timestamp") if c in new.columns][:1] + ["strike"]
    new = new.drop_duplicates(subset=key, keep="last")
    new.to_csv(out, index=False)


def pull_ticker(sym, events):
    tdir = OUT / sym
    tdir.mkdir(parents=True, exist_ok=True)
    marker = tdir / ".complete_earn"
    counts = {"files": 0, "empty": 0, "fail": 0, "skip": 0}
    if marker.exists():
        counts["skip"] = 1
        return counts
    text = _get("/v3/option/list/expirations", {"symbol": sym}, sym)
    if not text:
        marker.touch()
        return counts
    exps = []
    for line in text.splitlines()[1:]:
        parts = line.replace('"', "").split(",")
        if len(parts) >= 2:
            try:
                exps.append(date.fromisoformat(parts[1]))
            except ValueError:
                pass
    exps = sorted(set(exps))

    jobs = {}     # (kind, exp) -> (lo, hi)  — merge overlapping windows
    for e in events:
        for asof_off, win in ((-1, EVENT_WIN), (-CONTROL_OFFSET, CONTROL_WIN)):
            exp = nearest_expiry(exps, e + timedelta(days=asof_off))
            if exp is None:
                continue
            lo, hi = e + timedelta(days=win[0]), e + timedelta(days=win[1])
            for kind, _, _ in ENDPOINTS:
                k = (kind, exp)
                if k in jobs:
                    jobs[k] = (min(jobs[k][0], lo), max(jobs[k][1], hi))
                else:
                    jobs[k] = (lo, hi)

    for (kind, exp), (lo, hi) in sorted(jobs.items()):
        path, right = next((p, r) for kk, p, r in ENDPOINTS if kk == kind)
        out = tdir / f"{kind}_{exp.isoformat()}.csv"
        text = _get(path, {"symbol": sym, "expiration": exp.isoformat(),
                           "right": right, "start_date": lo.isoformat(),
                           "end_date": hi.isoformat()}, sym)
        if text is None:
            counts["fail"] += 1
        elif text and len(text.splitlines()) > 1:
            write_merged(out, text)
            counts["files"] += 1
        else:
            counts["empty"] += 1
    if counts["fail"] == 0:
        marker.touch()
    return counts


def main() -> int:
    earn = load_earnings()
    names = liquid_names(earn)
    work = {t: [e for e in earn[t] if WINDOW[0] <= e <= WINDOW[1]]
            for t in names}
    work = {t: v for t, v in work.items() if v}
    n_ev = sum(len(v) for v in work.values())
    log = open(DATA / "earnings_chains_pull.log", "a", encoding="utf-8",
               buffering=1)
    print(f"{len(work)} liquid names, {n_ev:,} events, "
          f"~{8 * n_ev:,} requests", file=log)
    totals = {"files": 0, "empty": 0, "fail": 0, "skip": 0, "crashed": 0}
    t0, done = time.time(), 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(pull_ticker, t, v): t for t, v in work.items()}
        for fut in as_completed(futs):
            try:
                c = fut.result()
            except RuntimeError:
                raise                              # tier wall — stop
            except Exception as e:
                totals["crashed"] += 1
                print(f"CRASH {futs[fut]}: {e!r}", file=log)
                c = {}
            for k in totals:
                totals[k] += c.get(k, 0)
            done += 1
            if done % 10 == 0:
                print(f"{done}/{len(work)} names ({(time.time()-t0)/3600:.1f}h) "
                      f"{totals}", file=log)
    print(f"EARNINGS CHAINS PULL DONE in {(time.time()-t0)/3600:.1f}h: {totals}",
          file=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
