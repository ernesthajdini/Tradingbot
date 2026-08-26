"""
FULL-STUDY PHASE 2 — as-of-date universe membership from the stock sweep.

Reads every backtest/data/thetadata_full/stocks/<SYM>.csv (as-traded EOD from
ThetaData) and computes, per trading day, which symbols the PRODUCTION
filters would have admitted: price inside the tier band AND 20-day average
volume >= MIN_DAILY_VOLUME. Exactly the filters.py rules, applied as-of —
including the names that later died (the survivorship rail).

Output: data/thetadata_full/members.json
  {
    "generated": ...,
    "params": {...},
    "tickers": {
      "<SYM>": {"sandbox": [["2016-03-01","2016-09-12"], ...],
                 "live":    [...]}
    }
  }
Interval-compressed (contiguous member days -> one interval; gaps > 7
calendar days start a new interval). Phase 3 pulls options for each ticker
over its intervals padded by DTE_MAX + 1 day.

Run AFTER the sweep completes:  python csp_screener/backtest/universe_members.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

from csp_screener import config

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
GAP_DAYS = 7  # calendar gap that splits membership intervals

BANDS = {
    "sandbox": (config.PRICE_MIN, config.PRICE_MAX),
    "live": (config.LIVE_PRICE_MIN, config.LIVE_PRICE_MAX),
}


def _intervals(dates: list[date]) -> list[list[str]]:
    if not dates:
        return []
    out = []
    start = prev = dates[0]
    for d in dates[1:]:
        if (d - prev).days > GAP_DAYS:
            out.append([start.isoformat(), prev.isoformat()])
            start = d
        prev = d
    out.append([start.isoformat(), prev.isoformat()])
    return out


def member_days(csv_path: Path) -> dict[str, list[date]] | None:
    try:
        df = pd.read_csv(csv_path, usecols=["created", "close", "volume"])
    except Exception:
        return None
    if df.empty or len(df) < 25:
        return None
    df["d"] = pd.to_datetime(df["created"], errors="coerce").dt.date
    df = df.dropna(subset=["d"]).sort_values("d")
    close = pd.to_numeric(df["close"], errors="coerce")
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    avg20 = vol.rolling(20).mean()
    liquid = avg20 >= config.MIN_DAILY_VOLUME
    out = {}
    for tier, (lo, hi) in BANDS.items():
        mask = liquid & (close >= lo) & (close <= hi)
        days = [d for d, m in zip(df["d"], mask) if m]
        if days:
            out[tier] = days
    return out or None


def main() -> int:
    t0 = time.time()
    stocks = sorted((DATA / "stocks").glob("*.csv"))
    print(f"{len(stocks)} stock histories to scan")
    tickers = {}
    day_counts = {"sandbox": 0, "live": 0}
    for i, p in enumerate(stocks, 1):
        md = member_days(p)
        if md:
            tickers[p.stem] = {t: _intervals(ds) for t, ds in md.items()}
            for t, ds in md.items():
                day_counts[t] += len(ds)
        if i % 2000 == 0:
            print(f"  {i}/{len(stocks)} scanned, {len(tickers)} members so far")
    payload = {
        "generated": date.today().isoformat(),
        "params": {
            "bands": BANDS, "min_volume": config.MIN_DAILY_VOLUME,
            "gap_days": GAP_DAYS,
            "note": "as-traded prices; 2016 has shortened warmup "
                    "(stock STANDARD starts 2016-01-01)",
        },
        "tickers": tickers,
    }
    out = DATA / "members.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    n_sand = sum(1 for v in tickers.values() if "sandbox" in v)
    n_live = sum(1 for v in tickers.values() if "live" in v)
    print(f"DONE in {time.time()-t0:.0f}s -> {out}")
    print(f"members-ever: sandbox {n_sand} tickers ({day_counts['sandbox']:,} "
          f"ticker-days), live {n_live} tickers ({day_counts['live']:,} "
          f"ticker-days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
