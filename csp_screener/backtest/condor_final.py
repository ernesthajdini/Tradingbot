"""
AMENDMENT 6 — condor management, then the binding third window.

Stage 1: the four declared management variants on TRAIN, then VALIDATE.
Stage 2: the THIRD WINDOW (2024-01-01 onward) — genuinely independent data
that neither the search nor the validation ever touched. Per Amendment 6B
the result is binding in BOTH directions and is reported whatever it says.

Every knob is proved to bite before any number is read (the lesson from two
inert-knob bugs).

    python csp_screener/backtest/condor_final.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np

from csp_screener.backtest import condor_study as cs
from csp_screener.backtest import data_loader
from csp_screener.backtest.day_store import DayStore

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
THIRD = (date(2024, 1, 1), date(2026, 8, 25))
CELL = dict(structure="iron_condor", scale=(10.0, 800.0), delta=0.20)
VARIANTS = [("stop2.0", 2.0, "close"), ("stop2.0+roll", 2.0, "roll"),
            ("nostop", None, "close"), ("nostop+roll", None, "roll")]


def load():
    store = DayStore(cs.STORE)
    cand = json.loads((DATA / "candidates_index.json").read_text(
        encoding="utf-8"))["days"]
    prices = {}
    for d in sorted(p for p in (DATA / "options_index").iterdir() if p.is_dir()):
        f = d / "stock_eod.csv"
        if f.exists():
            try:
                prices[d.name] = data_loader.load_thetadata_stock(f)
            except Exception:
                pass
    return store, cand, prices


def show(tag, r):
    if not r:
        print(f"  {tag:22} no trades")
        return None
    lo, hi = cs.boot_ci(r["pnls"])
    print(f"  {tag:22} n={r['n']:4} win={r['win']:.0%} "
          f"per-trade=${r['mean_pess']:7.2f} CI[{lo:7.2f},{hi:7.2f}] "
          f"total=${r['total_pess']:9.0f}")
    return lo, hi


def main() -> int:
    t0 = time.time()
    store, cand, prices = load()
    print(f"condor store: {len(store.dates)} days "
          f"({store.dates[0]} -> {store.dates[-1]})")
    print("generator: backtest_mirror (Amendment 5B)\n")

    # ---- knob-bite proof, before any result is read
    a = cs.run(CELL["structure"], CELL["scale"], CELL["delta"], store, cand,
               prices, cs.TRAIN, stop_mult=2.0, on_stop="close")
    b = cs.run(CELL["structure"], CELL["scale"], CELL["delta"], store, cand,
               prices, cs.TRAIN, stop_mult=None, on_stop="close")
    print(f"KNOB PROOF stop2.0 n={a['n']} mean=${a['mean_pess']:.2f} | "
          f"nostop n={b['n']} mean=${b['mean_pess']:.2f} -> "
          f"{'BITES' if (a['n'], a['mean_pess']) != (b['n'], b['mean_pess']) else 'INERT — ABORT'}")
    if (a["n"], a["mean_pess"]) == (b["n"], b["mean_pess"]):
        print("stop knob is inert; refusing to report results.")
        return 1

    print(f"\n=== STAGE 1: management variants, TRAIN "
          f"{cs.TRAIN[0]}..{cs.TRAIN[1]} (search, not evidence) ===")
    results = {}
    for tag, sm, os_ in VARIANTS:
        r = cs.run(CELL["structure"], CELL["scale"], CELL["delta"], store,
                   cand, prices, cs.TRAIN, stop_mult=sm, on_stop=os_)
        results[tag] = {"train": r}
        show(tag, r)

    print(f"\n=== STAGE 1: VALIDATE {cs.VALID[0]}..{cs.VALID[1]} ===")
    for tag, sm, os_ in VARIANTS:
        r = cs.run(CELL["structure"], CELL["scale"], CELL["delta"], store,
                   cand, prices, cs.VALID, stop_mult=sm, on_stop=os_)
        results[tag]["validate"] = r
        show(tag, r)

    print(f"\n=== STAGE 2: THIRD WINDOW {THIRD[0]}..{THIRD[1]} "
          f"(binding both ways, Amendment 6B) ===")
    verdicts = {}
    for tag, sm, os_ in VARIANTS:
        r = cs.run(CELL["structure"], CELL["scale"], CELL["delta"], store,
                   cand, prices, THIRD, stop_mult=sm, on_stop=os_)
        results[tag]["third"] = r
        ci = show(tag, r)
        if r and ci:
            verdicts[tag] = "POSITIVE" if ci[0] > 0 else (
                "NEGATIVE" if ci[1] < 0 else "INCONCLUSIVE")
        else:
            verdicts[tag] = "NO TRADES"

    print("\n=== VERDICT (Amendment 6B terms) ===")
    for tag, v in verdicts.items():
        print(f"  {tag:22} {v}")
    any_pos = any(v == "POSITIVE" for v in verdicts.values())
    print(f"\n  -> {'CONDOR IS THE FINDING — production rewrite + verification'
                   if any_pos else
                   'PREMIUM SELLING CLOSED as this account destination'}")

    out = DATA / "condor_final.json"
    out.write_text(json.dumps(
        {"generated": datetime.now().isoformat(), "cell": str(CELL),
         "verdicts": verdicts,
         "results": {k: {w: ({kk: vv for kk, vv in r.items() if kk != "pnls"}
                             if r else None)
                         for w, r in v.items()} for k, v in results.items()}},
        indent=1, default=str), encoding="utf-8")
    print(f"\n({(time.time()-t0)/60:.1f}min) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
