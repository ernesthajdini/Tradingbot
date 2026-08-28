"""
THE STUDY — pre-registered walk-forward over 2016-2023 (MANIFEST.md).

Runs the production gate code (filters, ranker order, setup generators,
sanity caps, friction band, exit rules, market-hours fill convention) over
the historical chains, for the declared grid ONLY:

    DTE window x target delta  = 2 x 3 = 6 cells, per tier.

HEADLINE = the production configuration (25-45 DTE, 0.30 delta). It is one
pre-registered test, so it carries no multiplicity penalty. The other five
cells are exploratory calibration and are read at alpha = 0.05/6.

Honesty rails enforced here (all stamped into every run record):
  * sealed period 2025+ is NOT touched (engine refuses without an explicit,
    logged unlock — the one-shot test happens later, once);
  * P&L is reported as a [pessimistic, base] band, never a point;
  * every run appends to runs_log.jsonl — that file IS the multiplicity
    denominator, crashed runs included;
  * ranking-source, survivorship, earnings/vix/ex-div gate coverage and
    missing-chain counts are printed with the results, not buried.

    python csp_screener/backtest/run_study.py [--tier sandbox|live|both]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from csp_screener.backtest import data_loader, engine
from csp_screener.backtest.day_store import DayStore

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
# The tradeable universe TRIPLES on 2017-02-08 (audit finding): 2016 and
# January 2017 are a data-availability artifact of the vendor's 2016-01-01
# floor, not study years — the RV percentile also has no 252-day warmup
# there. The exploration window starts once coverage is stable.
EXPLORE_FROM = date(2017, 2, 8)
EXPLORE_TO = date(2023, 12, 31)

DTE_WINDOWS = [(25, 45), (30, 45)]
DELTAS = [0.30, 0.25, 0.20]          # production first
PRODUCTION_CELL = ((25, 45), 0.30)


def load_prices() -> dict:
    out = {}
    for p in sorted((DATA / "stocks").glob("*.csv")):
        try:
            out[p.stem] = data_loader.load_thetadata_stock(p)
        except Exception:
            continue
    return out


def build_earnings_lookup():
    cal = {}
    for f in (DATA / "earnings").glob("*.csv"):
        try:
            cal[f.stem] = sorted(
                date.fromisoformat(l.strip())
                for l in f.read_text(encoding="utf-8").splitlines()
                if l.strip())
        except ValueError:
            continue

    def lookup(ticker, asof):
        nxt = next((d for d in cal.get(ticker, []) if d >= asof), None)
        return datetime.combine(nxt, datetime.min.time()) if nxt else None
    return lookup, len(cal)


def build_vix_lookup():
    cache = DATA / "vix.csv"
    if not cache.exists():
        import yfinance as yf
        df = yf.download("^VIX", start="2015-12-01", end="2026-08-26",
                         progress=False, auto_adjust=False)
        close = df["Close"]
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        close.dropna().to_csv(cache)
    import pandas as pd
    s = pd.read_csv(cache, index_col=0)
    vix = {}
    for k, v in zip(s.index, s.iloc[:, 0]):
        try:
            vix[date.fromisoformat(str(k)[:10])] = float(v)
        except (ValueError, TypeError):
            continue
    return (lambda asof: vix.get(asof)), len(vix)


def summarize(cell, tier, result) -> dict:
    s = result["summary"]
    return {
        "tier": tier,
        "dte": f"{cell[0][0]}-{cell[0][1]}",
        "delta": cell[1],
        "is_production": cell == PRODUCTION_CELL,
        "trades": s["closed_trades"],
        "win_rate": s["win_rate"],
        "pnl_base": s["total_pnl"],
        "pnl_pessimistic": s["total_pnl_pessimistic"],
        "per_trade_base": s["avg_pnl_per_trade"],
        "per_trade_pessimistic": s["avg_pnl_pessimistic_per_trade"],
        "market_marked": s["market_marked_share"],
        "data_ended": s.get("data_ended_closes"),
        "missing_chain_days": s.get("missing_chain_candidate_days"),
        "run_id": result["run_id"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="both",
                    choices=["sandbox", "live", "both"])
    args = ap.parse_args()
    tiers = ["sandbox", "live"] if args.tier == "both" else [args.tier]

    t0 = time.time()
    store = DayStore()
    cand = json.loads((DATA / "candidates.json").read_text(encoding="utf-8"))
    print(f"Day store: {len(store.dates)} days "
          f"({store.dates[0]} -> {store.dates[-1]})")
    prices = load_prices()
    earnings_lookup, n_earn = build_earnings_lookup()
    vix_lookup, n_vix = build_vix_lookup()
    print(f"{len(prices)} stock histories, {n_earn} earnings calendars, "
          f"{n_vix} VIX days")
    print(f"Exploration window: {EXPLORE_FROM} -> {EXPLORE_TO} "
          f"(sealed 2025+ untouched)\n")

    meta = {"source": str(DATA), "includes_delisted": True}
    rows = []
    for tier in tiers:
        for dte in DTE_WINDOWS:
            for delta in DELTAS:
                cell = (dte, delta)
                label = f"{tier} dte{dte[0]}-{dte[1]} d{delta:.2f}"
                print(f"--- {label} ---", flush=True)
                params = engine.BacktestParams(
                    dte_min=dte[0], dte_max=dte[1], target_delta=delta,
                    tier=tier, label=label)
                res = engine.run(
                    store, prices, params, source_meta=meta,
                    earnings_lookup=earnings_lookup, vix_lookup=vix_lookup,
                    candidates_by_date=cand["days"],
                    date_from=EXPLORE_FROM, date_to=EXPLORE_TO,
                )
                row = summarize(cell, tier, res)
                rows.append(row)
                print(f"    trades {row['trades']}, win {row['win_rate']}, "
                      f"band [${row['pnl_pessimistic']}, ${row['pnl_base']}], "
                      f"per-trade [${row['per_trade_pessimistic']}, "
                      f"${row['per_trade_base']}], "
                      f"mkt-marked {row['market_marked']}", flush=True)

    out = DATA / "study_results.json"
    out.write_text(json.dumps({
        "generated": datetime.now().isoformat(),
        "window": [EXPLORE_FROM.isoformat(), EXPLORE_TO.isoformat()],
        "production_cell": {"dte": "25-45", "delta": 0.30},
        "cells": rows,
    }, indent=1), encoding="utf-8")

    print(f"\n=== STUDY COMPLETE in {(time.time()-t0)/60:.1f}min -> {out} ===")
    for r in rows:
        star = " *PRODUCTION*" if r["is_production"] else ""
        print(f"{r['tier']:8} dte {r['dte']:6} d{r['delta']:.2f}  "
              f"n={r['trades']:5}  win={r['win_rate']}  "
              f"per-trade [{r['per_trade_pessimistic']}, "
              f"{r['per_trade_base']}]{star}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
