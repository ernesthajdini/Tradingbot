"""
FULL-STUDY PHASE 3b — historical earnings dates for the member universe.

Replays the production earnings-blackout gate. Source: yfinance
get_earnings_dates (past ~10 years for live names). KNOWN HONESTY GAP,
stamped into the study: Yahoo drops DELISTED tickers, so dead names mostly
arrive with no earnings dates -> the gate passes them (production's own
missing-data behavior) and the backtest holds through earnings production
might have blocked. Bias direction: PESSIMISTIC for the strategy (earnings
gaps land in the P&L) — the acceptable direction per the playbook. The
study report carries the coverage share so nobody mistakes it for full
replay.

Output: data/thetadata_full/earnings/<SYM>.csv (one ISO date per line).
Resumable; paced for Yahoo's rate limits.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
OUT = DATA / "earnings"
PACE = 0.6
LIMIT = 48  # quarters (~12 years)


def main() -> int:
    import yfinance as yf
    OUT.mkdir(parents=True, exist_ok=True)
    members = json.loads((DATA / "members.json").read_text(encoding="utf-8"))
    tickers = sorted(members["tickers"].keys())
    log = open(DATA / "earnings_pull.log", "a", encoding="utf-8", buffering=1)
    print(f"{len(tickers)} tickers", file=log)
    ok = none = skip = 0
    for i, sym in enumerate(tickers, 1):
        out = OUT / f"{sym}.csv"
        marker = OUT / f"{sym}.none"
        if out.exists() or marker.exists():
            skip += 1
            continue
        got = False
        for attempt in range(3):
            try:
                df = yf.Ticker(sym).get_earnings_dates(limit=LIMIT)
                if df is not None and len(df):
                    dates = sorted({d.date().isoformat() for d in df.index})
                    out.write_text("\n".join(dates) + "\n", encoding="utf-8")
                    ok += 1
                else:
                    marker.touch()
                    none += 1
                got = True
                break
            except Exception as e:
                msg = str(e)[:80]
                if "429" in msg or "Too Many" in msg:
                    time.sleep(30 * (attempt + 1))
                    continue
                marker.touch()
                none += 1
                got = True
                break
        if not got:
            marker.touch()
            none += 1
        time.sleep(PACE)
        if i % 200 == 0:
            print(f"{i}/{len(tickers)} ok={ok} none={none} skip={skip}",
                  file=log)
    print(f"EARNINGS PULL DONE: ok={ok} none={none} skip={skip} "
          f"coverage={ok/(ok+none):.0%}" if (ok + none) else "nothing new",
          file=log)
    print("EARNINGS PULL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
