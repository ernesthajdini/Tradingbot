"""
CALL MIRROR PULL — one call file for every put file already in the archive.

calls_pull.py walked the full membership windows for all 5,506 members and
reached 175 tickers in 22.8 hours — the put archive itself was pulled per
candidate expiry, so that design fetched an order of magnitude more than any
study can read. This pulls exactly the call twin of each put chain:

    options/<T>/puts_<EXP>.csv   ->  calls_<EXP>.csv     (same date range)
    options/<T>/oi_<EXP>.csv     ->  oi_calls_<EXP>.csv  (same date range)

~19k requests, resumable per file, crash-isolated per ticker, tier walls
fatal. Every study that needs calls (12: short call spreads, 15: the wheel's
covered-call leg, 14A's condor arm) reads only where puts already exist.

    python csp_screener/backtest/calls_mirror_pull.py
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

from csp_screener.backtest.calls_pull import DATA, WORKERS, _get

OUT = DATA / "options"


def _range(path: Path, col: str):
    try:
        s = pd.to_datetime(pd.read_csv(path, usecols=[col])[col], errors="coerce").dropna()
    except Exception:
        return None
    if s.empty:
        return None
    return s.min().date(), s.max().date()


def pull_ticker(sym: str) -> dict:
    tdir = OUT / sym
    marker = tdir / ".complete_calls_mirror"
    counts = {"calls": 0, "oi_calls": 0, "skip": 0, "empty": 0, "fail": 0}
    if marker.exists():
        counts["skip"] = 1
        return counts
    for pf in sorted(tdir.glob("puts_*.csv")):
        if pf.stat().st_size < 50:
            continue
        exp = pf.stem.replace("puts_", "")
        jobs = [("calls", "/v3/option/history/eod", _range(pf, "created"),
                 tdir / f"calls_{exp}.csv")]
        oif = tdir / f"oi_{exp}.csv"
        if oif.exists() and oif.stat().st_size > 50:
            jobs.append(("oi_calls", "/v3/option/history/open_interest",
                         _range(oif, "timestamp"), tdir / f"oi_calls_{exp}.csv"))
        for kind, path, rng, out in jobs:
            if out.exists() or rng is None:
                counts["skip"] += out.exists()
                continue
            text = _get(path, {"symbol": sym, "expiration": exp, "right": "call",
                               "start_date": rng[0].isoformat(),
                               "end_date": rng[1].isoformat()}, sym)
            if text is None:
                counts["fail"] += 1
            elif text and len(text.splitlines()) > 1:
                out.write_text(text, encoding="utf-8")
                counts[kind] += 1
            else:
                out.write_text("", encoding="utf-8")       # tried, no data
                counts["empty"] += 1
    if counts["fail"] == 0:
        marker.touch()
    return counts


def main() -> int:
    tickers = sorted(p.name for p in OUT.iterdir()
                     if p.is_dir() and any(p.glob("puts_*.csv")))
    log = open(DATA / "calls_mirror_pull.log", "a", encoding="utf-8", buffering=1)
    print(f"{len(tickers)} tickers with put chains to mirror", file=log)
    totals = {"calls": 0, "oi_calls": 0, "skip": 0, "empty": 0, "fail": 0,
              "crashed": 0}
    t0, done = time.time(), 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(pull_ticker, t): t for t in tickers}
        for fut in as_completed(futs):
            try:
                c = fut.result()
            except RuntimeError:
                raise                                   # tier wall
            except Exception as e:
                totals["crashed"] += 1
                print(f"CRASH {futs[fut]}: {e!r}", file=log)
                c = {}
            for k in totals:
                totals[k] += c.get(k, 0)
            done += 1
            if done % 50 == 0:
                print(f"{done}/{len(tickers)} tickers ({(time.time()-t0)/3600:.1f}h) "
                      f"{totals}", file=log)
    print(f"CALL MIRROR DONE in {(time.time()-t0)/3600:.1f}h: {totals}", file=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
