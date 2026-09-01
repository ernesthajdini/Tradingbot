"""
The structural measurements that explain every amendment verdict.

These are facts about the MARKET, not about any one strategy, so they are
measured once by this script and written to friction_measurements.json rather
than quoted from memory anywhere. The dashboard renders that file; nothing
downstream retypes a number.

Produces:
  1. Round-trip toll by universe (single name vs index ETF), matched on
     delta, DTE and open interest.
  2. Horizon decay of the 52-week-high signal — the test that closed
     long-dated structures without spending a data pull.

    python csp_screener/backtest/measure_structure.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd
from scipy import stats

from csp_screener.backtest.condor_study import STORE as ISTORE
from csp_screener.backtest.day_store import DayStore, STORE
from csp_screener.backtest.pattern_study import (DATA, MIN_PRICE, MIN_VOL,
                                                 load_ohlc)

TRAIN = (date(2017, 2, 8), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))
HORIZONS = [21, 63, 126, 252]
COMMISSION = 1.00


def spread_tax(store_path, label, every=25):
    """Median bid-ask on comparable contracts, as % of premium round trip."""
    store = DayStore(store_path)
    chunks = []
    for d in [x for x in store.dates if 2017 <= x.year <= 2023][::every]:
        r = store.day(d)
        dte = np.array([(e - d).days for e in r["expiration"]])
        q = r[(dte >= 20) & (dte <= 60) & (r["bid"] > 0) & (r["ask"] > 0)
              & (r["open_interest"] >= 100) & r["delta"].notna()]
        q = q[(q["delta"].abs() >= 0.25) & (q["delta"].abs() <= 0.45)]
        if len(q):
            chunks.append(q[["bid", "ask"]])
    q = pd.concat(chunks)
    mid, spr = (q["bid"] + q["ask"]) / 2, q["ask"] - q["bid"]
    round_trip = float(spr.median()) * 100 + 2 * COMMISSION
    return {
        "universe": label, "n_contracts": int(len(q)),
        "median_mid": round(float(mid.median()), 2),
        "median_spread": round(float(spr.median()), 2),
        "median_spread_pct_of_mid": round(float((100 * spr / mid).median()), 1),
        "round_trip_usd": round(round_trip, 2),
        "round_trip_pct_of_premium": round(
            100 * round_trip / (float(mid.median()) * 100), 1),
    }


def horizon_decay():
    """Does the 52-week-high effect survive past 21 days? (It does not.)"""
    acc = {w: {} for w in ("train", "valid")}
    files = sorted((DATA / "stocks").glob("*.csv"))
    for i, p in enumerate(files, 1):
        try:
            df = load_ohlc(p)
        except Exception:
            continue
        if len(df) < 600:
            continue
        c, h, v = df["c"], df["h"], df["v"]
        elig = (c >= MIN_PRICE) & (v.shift(1).rolling(20).mean() >= MIN_VOL)
        hi = c > h.shift(1).rolling(252).max()
        dd = df.index.date
        for wn, (a, b) in (("train", TRAIN), ("valid", VALID)):
            win = elig & (dd >= a) & (dd <= b)
            if not win.any():
                continue
            for z in HORIZONS:
                f = c.shift(-z) / c - 1.0
                base = f[win].dropna()
                if len(base):
                    acc[wn].setdefault(("B", z), []).append(base.to_numpy())
                sel = f[win & hi.fillna(False)].dropna()
                if len(sel):
                    acc[wn].setdefault(("H", z), []).append(sel.to_numpy())
        if i % 2500 == 0:
            print(f"  horizon {i}/{len(files)}", flush=True)

    rows = []
    for wn in ("train", "valid"):
        for z in HORIZONS:
            if ("H", z) not in acc[wn]:
                continue
            s = np.concatenate(acc[wn][("H", z)])
            b = np.concatenate(acc[wn][("B", z)])
            _, pv = stats.ttest_ind(s, b, equal_var=False)
            edge = 100 * (float(s.mean()) - float(b.mean()))
            rows.append({"window": wn, "horizon_days": z, "n": int(len(s)),
                         "edge_pct": round(edge, 2),
                         "edge_pct_per_month": round(edge / (z / 21.0), 2),
                         "p": float(pv)})
    return rows


def main() -> int:
    t0 = time.time()
    print("measuring round-trip toll by universe...", flush=True)
    taxes = [spread_tax(STORE, "single names"),
             spread_tax(ISTORE, "index ETFs")]
    for t in taxes:
        print(f"  {t['universe']:14} spread ${t['median_spread']:.2f} on "
              f"${t['median_mid']:.2f} mid -> "
              f"{t['round_trip_pct_of_premium']}% of premium", flush=True)

    print("\nmeasuring 52-week-high horizon decay...", flush=True)
    decay = horizon_decay()
    for r in decay:
        if r["window"] == "valid":
            print(f"  valid {r['horizon_days']:4}d "
                  f"{r['edge_pct_per_month']:+6.2f}%/mo  p={r['p']:.1e}")

    out = DATA / "friction_measurements.json"
    out.write_text(json.dumps({
        "generated": datetime.now().isoformat(),
        "method": ("contracts matched on |delta| 0.25-0.45, DTE 20-60, "
                   "open interest >= 100, 2017-2023; commission "
                   f"${COMMISSION:.2f} per contract per leg"),
        "round_trip_toll": taxes,
        "signal_horizon_decay": decay,
    }, indent=1), encoding="utf-8")
    print(f"\n({(time.time()-t0)/60:.1f}min) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
