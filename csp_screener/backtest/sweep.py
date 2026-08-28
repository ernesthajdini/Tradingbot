"""
PHASE 2 SEARCH — the declared space in MANIFEST Amendment 1, run honestly.

TRAIN 2017-02-08..2021-12-31 -> promotion rule -> VALIDATE 2022..2023.
Sealed 2024+ never touched. Every run lands in runs_log.jsonl, which IS the
multiplicity denominator. Train numbers are SEARCH RESULTS, not evidence;
only validation numbers may be quoted as findings, at alpha = 0.05/promoted.

    python csp_screener/backtest/sweep.py [--universe single_name|index_etf]
"""
from __future__ import annotations
import argparse, itertools, json, sys, time
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import numpy as np
from csp_screener.backtest import data_loader, engine
from csp_screener.backtest.day_store import DayStore

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
TRAIN = (date(2017, 2, 8), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))
DTES = [(25, 45), (30, 45)]
DELTAS = [0.30, 0.25, 0.20, 0.15]
EXITS = [21, 7, 0]
STOPS = [2.0, 3.0, None]
# universe x structure -> production tier (index+csp is declared SKIPPED)
COMBOS = {("single_name", "csp"): "sandbox", ("single_name", "spread"): "live",
          ("index_etf", "spread"): "live"}
MIN_TRAIN_TRADES = 100
MAX_SINGLE_TRADE_SHARE = 0.40


def load_prices(root: Path, sub: str = "stocks") -> dict:
    out = {}
    src = root / sub
    if sub == "stocks":
        files = sorted(src.glob("*.csv"))
        for p in files:
            try: out[p.stem] = data_loader.load_thetadata_stock(p)
            except Exception: pass
    else:
        for d in sorted(p for p in src.iterdir() if p.is_dir()):
            f = d / "stock_eod.csv"
            if f.exists():
                try: out[d.name] = data_loader.load_thetadata_stock(f)
                except Exception: pass
    return out


def stats(trades, basis="pnl_pessimistic"):
    if not trades: return None
    a = np.array([t[basis] for t in trades], dtype=float)
    top = abs(a.min()) / abs(a.sum()) if a.sum() else float("inf")
    return {"n": len(a), "mean": float(a.mean()), "median": float(np.median(a)),
            "total": float(a.sum()), "win": float((a > 0).mean()),
            "worst_share": float(top)}


def boot_ci(trades, basis="pnl_pessimistic", iters=10000, seed=7):
    a = np.array([t[basis] for t in trades], dtype=float)
    rng = np.random.default_rng(seed)
    bs = np.array([rng.choice(a, len(a), replace=True).mean() for _ in range(iters)])
    return float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="single_name")
    args = ap.parse_args()
    uni = args.universe

    store = DayStore(DATA / ("daystore" if uni == "single_name" else "daystore_index"))
    cfile = "candidates.json" if uni == "single_name" else "candidates_index.json"
    cand = json.loads((DATA / cfile).read_text(encoding="utf-8"))["days"]
    prices = load_prices(DATA, "stocks" if uni == "single_name" else "options_index")
    from csp_screener.backtest.run_study import build_earnings_lookup, build_vix_lookup
    el, _ = build_earnings_lookup(); vl, _ = build_vix_lookup()
    meta = {"includes_delisted": uni == "single_name"}
    print(f"{uni}: {len(store.dates)} days, {len(prices)} price frames", flush=True)

    structures = [s for (u, s) in COMBOS if u == uni]
    space = [(st, d, dl, ex, sp) for st in structures for d in DTES
             for dl in DELTAS for ex in EXITS for sp in STOPS]
    print(f"declared space for {uni}: {len(space)} configurations\n", flush=True)

    results, t0 = [], time.time()
    for i, (st, dte, dl, ex, sp) in enumerate(space, 1):
        label = f"{uni}/{st} dte{dte[0]}-{dte[1]} d{dl:.2f} exit{ex} stop{sp}"
        p = engine.BacktestParams(dte_min=dte[0], dte_max=dte[1], target_delta=dl,
                                  tier=COMBOS[(uni, st)], exit_dte=ex, stop_mult=sp,
                                  universe=uni, label=label)
        r = engine.run(store, prices, p, source_meta=meta, earnings_lookup=el,
                       vix_lookup=vl, candidates_by_date=cand,
                       date_from=TRAIN[0], date_to=TRAIN[1], write_results=False)
        s = stats(r["trades"])
        row = {"label": label, "universe": uni, "structure": st,
               "dte": f"{dte[0]}-{dte[1]}", "delta": dl, "exit_dte": ex,
               "stop": sp, "train": s, "trades": r["trades"]}
        results.append(row)
        if i % 12 == 0 or i == len(space):
            print(f"  {i}/{len(space)} ({(time.time()-t0)/60:.0f}min)", flush=True)

    # ---- promotion rule (fixed in MANIFEST Amendment 1 section C)
    promoted = [r for r in results if r["train"] and r["train"]["n"] >= MIN_TRAIN_TRADES
                and r["train"]["mean"] > 0 and r["train"]["median"] > 0
                and r["train"]["worst_share"] <= MAX_SINGLE_TRADE_SHARE]
    print(f"\nTRAIN complete: {len(results)} configs, {len(promoted)} promoted", flush=True)

    findings = []
    for r in promoted:
        p = engine.BacktestParams(
            dte_min=int(r["dte"].split("-")[0]), dte_max=int(r["dte"].split("-")[1]),
            target_delta=r["delta"], tier=COMBOS[(uni, r["structure"])],
            exit_dte=r["exit_dte"], stop_mult=r["stop"], universe=uni,
            label="VALIDATE " + r["label"])
        v = engine.run(store, prices, p, source_meta=meta, earnings_lookup=el,
                       vix_lookup=vl, candidates_by_date=cand,
                       date_from=VALID[0], date_to=VALID[1], write_results=False)
        vs = stats(v["trades"])
        ci = boot_ci(v["trades"]) if v["trades"] else (0.0, 0.0)
        r["validate"] = vs; r["validate_ci"] = ci
        findings.append(r)

    alpha_note = f"alpha = 0.05/{max(len(promoted),1)}"
    out = {"generated": datetime.now().isoformat(), "universe": uni,
           "space_size": len(space), "train": [d.isoformat() for d in TRAIN],
           "validate": [d.isoformat() for d in VALID], "alpha": alpha_note,
           "configs": [{k: v for k, v in r.items() if k != "trades"} for r in results]}
    (DATA / f"sweep_{uni}.json").write_text(json.dumps(out, indent=1, default=str),
                                            encoding="utf-8")

    print(f"\n=== TOP 12 BY TRAIN (search results, NOT evidence) ===")
    for r in sorted([x for x in results if x["train"]],
                    key=lambda x: -x["train"]["mean"])[:12]:
        s = r["train"]
        print(f"  {r['label']:56} n={s['n']:4} pess/trade=${s['mean']:7.2f} "
              f"win={s['win']:.0%} worst={s['worst_share']:.0%}")
    print(f"\n=== PROMOTED -> VALIDATION ({alpha_note}) ===")
    if not findings:
        print("  none — no configuration met the pre-registered promotion rule")
    for r in sorted(findings, key=lambda x: -(x["validate"]["mean"] if x["validate"] else -99)):
        v = r["validate"]
        if not v: print(f"  {r['label']:56} no validation trades"); continue
        lo, hi = r["validate_ci"]
        verdict = "FINDING" if lo > 0 else "fails validation"
        print(f"  {r['label']:56} n={v['n']:4} pess/trade=${v['mean']:7.2f} "
              f"CI[{lo:7.2f},{hi:7.2f}] {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
