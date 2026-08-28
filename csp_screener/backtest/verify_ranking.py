"""
FULL-STUDY PHASE 4b — exactness proof for the precomputed ranking.

The study feeds engine.run a precomputed daily top-N (candidates.json)
instead of re-deriving it from 10k ticker-histories on every one of 2,651
days x 6 config cells. That is only legitimate if the precompute reproduces
what the engine's OWN filter+rank path would have produced.

This script proves it on a random sample of days: for each sampled day it
runs the engine's real path — universe_asof -> TickerContext ->
filters.apply_all_filters -> ranker.rank_candidates — over the full stock
universe, and compares the resulting top-N per tier against candidates.json.

Any mismatch is printed with both orderings. Exit code 1 on ANY divergence:
the study runner refuses to proceed unless this passes.

    python csp_screener/backtest/verify_ranking.py [--days 25] [--seed 7]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd

from csp_screener import config, data_pipeline, filters, ranker
from csp_screener.backtest import data_loader
from csp_screener.filters import TickerContext

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
BANDS = {
    "sandbox": (config.PRICE_MIN, config.PRICE_MAX),
    "live": (config.LIVE_PRICE_MIN, config.LIVE_PRICE_MAX),
}


def load_prices() -> dict[str, pd.DataFrame]:
    out = {}
    for p in sorted((DATA / "stocks").glob("*.csv")):
        try:
            out[p.stem] = data_loader.load_thetadata_stock(p)
        except Exception:
            continue
    return out


def load_earnings() -> dict[str, list[date]]:
    out = {}
    for f in (DATA / "earnings").glob("*.csv"):
        try:
            out[f.stem] = sorted(
                date.fromisoformat(l.strip())
                for l in f.read_text(encoding="utf-8").splitlines()
                if l.strip())
        except ValueError:
            continue
    return out


def engine_ranking(prices, earnings, asof: date, tier: str) -> list[str]:
    """The engine's own path, verbatim."""
    lo, hi = BANDS[tier]
    asof_dt = datetime.combine(asof, datetime.min.time())
    contexts = []
    for ticker, df in prices.items():
        try:
            hist = df[df.index.date <= asof]
        except (AttributeError, TypeError):
            continue
        if hist.empty:
            continue
        # Same staleness gate as universe_asof — a dead ticker is not a
        # candidate on a date years after its last bar.
        if (asof - hist.index[-1].date()).days > 0:
            continue
        px = data_pipeline.last_price(hist)
        if px is None or not (lo <= px <= hi):
            continue
        edates = earnings.get(ticker) or []
        nxt = next((d for d in edates if d >= asof), None)
        contexts.append(TickerContext(
            ticker=ticker,
            last_price=px,
            avg_volume_20d=data_pipeline.avg_volume_20d(hist) or 0.0,
            next_earnings=(datetime.combine(nxt, datetime.min.time())
                           if nxt else None),
            price_history=(hist["Adj Close"] if "Adj Close" in hist.columns
                           else hist["Close"]).copy(),
            price_min=lo, price_max=hi,
        ))
    passing = []
    for ctx in contexts:
        if not filters.apply_all_filters(ctx, now=asof_dt).passed:
            continue
        ned = ((ctx.next_earnings - asof_dt).days
               if ctx.next_earnings is not None else None)
        passing.append({
            "ticker": ctx.ticker, "last_price": ctx.last_price,
            "avg_volume_20d": ctx.avg_volume_20d,
            "next_earnings_days": ned, "price_history": ctx.price_history,
        })
    return [c.ticker for c in ranker.rank_candidates(
        passing, top_n=config.MAX_CANDIDATES_IN_EMAIL)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    cand = json.loads((DATA / "candidates.json").read_text(encoding="utf-8"))
    days = sorted(cand["days"].keys())
    sample = random.Random(args.seed).sample(days, min(args.days, len(days)))
    print(f"Loading stock universe...", flush=True)
    prices = load_prices()
    earnings = load_earnings()
    print(f"{len(prices)} histories, {len(earnings)} earnings calendars")
    print(f"Verifying {len(sample)} sampled days against the engine path\n")

    mismatches = 0
    checked = 0
    for day in sorted(sample):
        asof = date.fromisoformat(day)
        for tier in ("sandbox", "live"):
            want = cand["days"][day].get(tier, [])
            got = engine_ranking(prices, earnings, asof, tier)
            checked += 1
            if want != got:
                mismatches += 1
                print(f"MISMATCH {day} [{tier}]")
                print(f"  precompute: {want}")
                print(f"  engine    : {got}")
        print(f"  {day} checked", flush=True)

    print(f"\n{checked - mismatches}/{checked} day-tiers agree exactly")
    if mismatches:
        print("RANKING VERIFICATION FAILED — study must not use the "
              "precomputed path until this is reconciled.")
        return 1
    print("RANKING VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
