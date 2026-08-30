"""
AMENDMENT 4 — regime-conditional premium selling.

Does WHEN you sell matter? Everything is held at production values
(25-45 DTE, 0.30 delta, 21-DTE exit, 2x stop); only the entry regime varies.
The playbook's post-spike window has never been tested because the VIX kill
switch blocks entries above 35, so the 2020 crash contributes zero trades to
every prior result.

Power limit declared in MANIFEST Amendment 4 before running: post_spike
qualifies 46 of 1,735 sessions (2.7%), so at 13-17 trades/year it is
expected to be UNTESTABLE. Trade counts are printed for every arm so that
shows up as a measured fact rather than an absence.

    python csp_screener/backtest/regime_study.py
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

from csp_screener.backtest import data_loader, engine
from csp_screener.backtest.day_store import DayStore

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
TRAIN = (date(2017, 2, 8), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))
REGIMES = ["none", "post_spike", "vix_above_25", "vix_top_quartile",
           "vix_falling"]
ARMS = [("single_name", "sandbox", (2.0, 130.0)),
        ("index_etf", "live", (5.0, 400.0))]


def regime_masks():
    """One boolean series per declared regime, from the VIX close series."""
    s = pd.read_csv(DATA / "vix.csv", index_col=0)
    vix = pd.Series(s.iloc[:, 0].values,
                    index=pd.to_datetime(s.index)).dropna().sort_index()
    m = {
        "none": pd.Series(True, index=vix.index),
        "post_spike": (vix.rolling(10).max() > 35) & (vix < 30),
        "vix_above_25": vix > 25,
        "vix_top_quartile": vix.rolling(252, min_periods=60).rank(pct=True) >= 0.75,
        "vix_falling": vix < vix.rolling(10).mean(),
    }
    return {k: {d.date(): bool(v) for d, v in ser.fillna(False).items()}
            for k, ser in m.items()}


def load_arm(universe):
    if universe == "single_name":
        store = DayStore(DATA / "daystore")
        cand = json.loads((DATA / "candidates.json").read_text(
            encoding="utf-8"))["days"]
        prices = {}
        for p in sorted((DATA / "stocks").glob("*.csv")):
            try:
                prices[p.stem] = data_loader.load_thetadata_stock(p)
            except Exception:
                pass
    else:
        store = DayStore(DATA / "daystore_index")
        cand = json.loads((DATA / "candidates_index.json").read_text(
            encoding="utf-8"))["days"]
        prices = {}
        for d in sorted(p for p in (DATA / "options_index").iterdir()
                        if p.is_dir()):
            f = d / "stock_eod.csv"
            if f.exists():
                try:
                    prices[d.name] = data_loader.load_thetadata_stock(f)
                except Exception:
                    pass
    return store, cand, prices


def boot_ci(vals, iters=10000, seed=13):
    a = np.asarray(vals, dtype=float)
    rng = np.random.default_rng(seed)
    bs = np.array([rng.choice(a, len(a), replace=True).mean()
                   for _ in range(iters)])
    return float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def main() -> int:
    t0 = time.time()
    masks = regime_masks()
    from csp_screener.backtest.run_study import (build_earnings_lookup,
                                                 build_vix_lookup)
    el, _ = build_earnings_lookup()
    vl, _ = build_vix_lookup()

    rows = []
    for universe, tier, scale in ARMS:
        store, cand, prices = load_arm(universe)
        meta = {"includes_delisted": universe == "single_name"}
        print(f"\n=== {universe} ({len(store.dates)} days, "
              f"{len(prices)} price frames) ===", flush=True)
        for reg in REGIMES:
            mask = masks[reg]
            p = engine.BacktestParams(
                tier=tier, universe=universe, scale=scale, regime=reg,
                label=f"{universe}/{reg}")
            r = engine.run(store, prices, p, source_meta=meta,
                           earnings_lookup=el, vix_lookup=vl,
                           candidates_by_date=cand,
                           regime_lookup=lambda d, m=mask: m.get(d, False),
                           date_from=TRAIN[0], date_to=TRAIN[1],
                           write_results=False)
            s = r["summary"]
            rows.append({"universe": universe, "regime": reg,
                         "trades": s["closed_trades"],
                         "per_trade_pess": s["avg_pnl_pessimistic_per_trade"],
                         "per_trade_base": s["avg_pnl_per_trade"],
                         "total_pess": s["total_pnl_pessimistic"],
                         "win": s["win_rate"],
                         "trades_obj": r["trades"]})
            print(f"  {reg:18} trades={s['closed_trades']:4} "
                  f"win={s['win_rate']} "
                  f"per-trade [{s['avg_pnl_pessimistic_per_trade']}, "
                  f"{s['avg_pnl_per_trade']}]", flush=True)

    promoted = [r for r in rows if r["trades"] >= 100
                and (r["per_trade_pess"] or 0) > 0]
    print(f"\nTRAIN complete ({(time.time()-t0)/60:.1f}min): "
          f"{len(rows)} arms, {len(promoted)} promoted "
          f"(bar: >=100 trades and positive at pessimistic fills)")
    if not promoted:
        print("  none — no regime arm reached a testable sample with a "
              "positive pessimistic mean")

    out = DATA / "regime_study.json"
    out.write_text(json.dumps(
        {"generated": datetime.now().isoformat(),
         "train": [d.isoformat() for d in TRAIN],
         "arms": [{k: v for k, v in r.items() if k != "trades_obj"}
                  for r in rows]}, indent=1, default=str), encoding="utf-8")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
