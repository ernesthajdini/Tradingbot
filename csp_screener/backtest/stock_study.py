"""
AMENDMENT 3 — equity signal study.

Long-only cross-sectional signals over the optionable universe already
downloaded (delisted names included). Options remain the destination: the
question here is whether ANY declared signal sorts future returns, because
that is the input the options screener never had — it ranks candidates by
realized-vol percentile, and the 8-year study produced no evidence that
ranking selects winners. A validated signal is what a future options
strategy would be built ON.

Friction is charged on every entry and exit in the same [base, pessimistic]
band convention the options studies use: $1.00 commission per trade plus
0.10% (base) / 0.25% (pessimistic) of notional. At 5 positions of ~$240
that is ~0.8% per rebalance — the honest headwind a small account faces.

Benchmark: SPY buy-and-hold over the identical window.

    python csp_screener/backtest/stock_study.py
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

from csp_screener.backtest import data_loader

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
TRAIN = (date(2017, 2, 8), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))

SIGNALS = ["mom_12_1", "rev_1m", "trend_200", "lowvol_20"]
HOLDS = [21, 63]
SIZES = [5, 10]

COMMISSION = 1.0
SLIP = {"base": 0.0010, "pess": 0.0025}
MIN_PRICE, MIN_VOL = 5.0, 1_000_000
EQUITY = 1200.0


def load_panel():
    """Wide close / adjusted-close / volume panels over every history."""
    closes, adjs, vols = {}, {}, {}
    for p in sorted((DATA / "stocks").glob("*.csv")):
        try:
            df = data_loader.load_thetadata_stock(p)
        except Exception:
            continue
        if len(df) < 260:
            continue
        closes[p.stem] = df["Close"]
        adjs[p.stem] = (df["Adj Close"] if "Adj Close" in df.columns
                        else df["Close"])
        vols[p.stem] = df["Volume"]
    C = pd.DataFrame(closes).sort_index()
    A = pd.DataFrame(adjs).sort_index().reindex(C.index)
    V = pd.DataFrame(vols).sort_index().reindex(C.index)
    return C, A, V


def signal_frame(name, A):
    """Higher = more preferred. Every value uses only prior data."""
    if name == "mom_12_1":       # 12-month return, skipping the last month
        return A.shift(21) / A.shift(252) - 1.0
    if name == "rev_1m":         # short-term reversal: prefer 1-month losers
        return -(A / A.shift(21) - 1.0)
    if name == "trend_200":      # distance above the 200-day average
        return A / A.rolling(200).mean() - 1.0
    if name == "lowvol_20":      # prefer low realized volatility
        return -(np.log(A / A.shift(1)).rolling(20).std())
    raise ValueError(name)


def run(hold, size, C, V, S, window, band="pess"):
    """Equal-weight long-only portfolio, rebalanced every `hold` sessions."""
    slip = SLIP[band]
    idx = [d for d in C.index if window[0] <= d.date() <= window[1]]
    if len(idx) < hold + 1:
        return None
    rebal = idx[::hold]

    equity = EQUITY
    positions, curve = [], []
    data_ended = 0
    blown = False
    # A long-only account cannot go below zero, and cannot keep trading once
    # a position is too small to carry a $2 round-trip commission. Without
    # this floor the simulation kept "trading" a depleted balance and printed
    # returns past -100%, which is arithmetically impossible.
    MIN_VIABLE_STAKE = 50.0

    for i, d in enumerate(rebal[:-1]):
        nxt = rebal[i + 1]
        px = C.loc[d]
        v20 = V.loc[:d].tail(20).mean()
        elig = px.notna() & (px >= MIN_PRICE) & (v20 >= MIN_VOL)
        s = S.loc[d][elig].dropna()
        if len(s) < size or equity < size * MIN_VIABLE_STAKE:
            if equity < size * MIN_VIABLE_STAKE:
                blown = True
                curve.append((d.date(), equity))
                break
            curve.append((d.date(), equity))
            continue
        picks = s.nlargest(size).index.tolist()
        stake = equity / size

        period_pnl = 0.0
        for t in picks:
            p0 = C.at[d, t]
            if not np.isfinite(p0) or p0 <= 0:
                continue
            fut = C.loc[nxt:, t].dropna()
            if fut.empty:
                # Stopped trading during the hold — exit at the last print.
                # Bankruptcies collapse before delisting and acquisitions
                # settle near the deal price, so the last print is the
                # honest fill; the count is reported, never hidden.
                tail = C.loc[d:, t].dropna()
                if len(tail) < 2:
                    continue
                p1 = float(tail.iloc[-1])
                data_ended += 1
            else:
                p1 = float(fut.iloc[0])
            shares = stake / p0
            gross = shares * (p1 - p0)
            fric = 2 * COMMISSION + slip * (shares * p0 + shares * p1)
            pnl = gross - fric
            positions.append(pnl)
            period_pnl += pnl

        equity += period_pnl
        curve.append((d.date(), equity))

    if not positions:
        return None
    a = np.array(positions)
    eq = np.array([c[1] for c in curve]) if curve else np.array([EQUITY])
    peak = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / peak).min())
    return {
        "n": len(a), "total": float(a.sum()), "mean": float(a.mean()),
        "median": float(np.median(a)), "win": float((a > 0).mean()),
        "final_equity": float(equity),
        "return_pct": float(100 * (equity - EQUITY) / EQUITY),
        "max_dd": max_dd, "data_ended": data_ended, "blown": blown,
        "positions": [float(x) for x in a],
    }


def spy_benchmark(C, window):
    if "SPY" not in C.columns:
        return None
    s = C["SPY"].dropna()
    s = s[(s.index.date >= window[0]) & (s.index.date <= window[1])]
    if len(s) < 2:
        return None
    eq = (EQUITY * (s / s.iloc[0])).to_numpy()
    peak = np.maximum.accumulate(eq)
    return {"return_pct": float(100 * (s.iloc[-1] / s.iloc[0] - 1)),
            "max_dd": float(((eq - peak) / peak).min())}


def boot_ci(vals, iters=10000, seed=11):
    a = np.asarray(vals, dtype=float)
    rng = np.random.default_rng(seed)
    bs = np.array([rng.choice(a, len(a), replace=True).mean()
                   for _ in range(iters)])
    return float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def main() -> int:
    t0 = time.time()
    print("loading panel...", flush=True)
    C, A, V = load_panel()
    print(f"{C.shape[1]} tickers x {C.shape[0]} sessions", flush=True)
    bt, bv = spy_benchmark(C, TRAIN), spy_benchmark(C, VALID)
    print(f"SPY train {bt['return_pct']:.1f}% (dd {bt['max_dd']:.1%}) | "
          f"validate {bv['return_pct']:.1f}% (dd {bv['max_dd']:.1%})\n",
          flush=True)

    rows = []
    for sig in SIGNALS:
        S = signal_frame(sig, A)
        for hold in HOLDS:
            for size in SIZES:
                r = run(hold, size, C, V, S, TRAIN, "pess")
                rb = run(hold, size, C, V, S, TRAIN, "base")
                if r is None:
                    continue
                rows.append({"signal": sig, "hold": hold, "size": size,
                             "train_pess": r, "train_base": rb})
                print(f"  {sig:10} hold{hold:3} n{size:3}  pos={r['n']:5} "
                      f"ret={r['return_pct']:8.1f}% "
                      f"(base {rb['return_pct']:8.1f}%) "
                      f"dd={r['max_dd']:7.1%} win={r['win']:.0%} "
                      f"ended={r['data_ended']}"
                      f"{' BLOWN' if r['blown'] else ''}", flush=True)

    promoted = [r for r in rows
                if not r["train_pess"]["blown"]
                and r["train_pess"]["n"] >= 100
                and r["train_pess"]["total"] > 0
                and r["train_pess"]["return_pct"] > bt["return_pct"]
                and abs(r["train_pess"]["max_dd"]) <= 1.5 * abs(bt["max_dd"])]
    print(f"\nTRAIN complete ({(time.time()-t0)/60:.1f}min): {len(rows)} "
          f"configs, {len(promoted)} promoted")
    print(f"  bar: >=100 positions, positive at pessimistic fills, "
          f"beats SPY {bt['return_pct']:.1f}%, dd <= {1.5*abs(bt['max_dd']):.1%}")

    alpha = 0.05 / max(len(promoted), 1)
    print(f"\n=== VALIDATION (2022-2023, alpha = {alpha:.4f}) ===")
    if not promoted:
        print("  none — no declared signal met the pre-registered bar")
    for r in promoted:
        S = signal_frame(r["signal"], A)
        v = run(r["hold"], r["size"], C, V, S, VALID, "pess")
        r["validate_pess"] = v
        if not v:
            print(f"  {r['signal']} hold{r['hold']} n{r['size']}: no positions")
            continue
        lo, hi = boot_ci(v["positions"])
        beats = v["return_pct"] > bv["return_pct"]
        ok = beats and v["total"] > 0 and lo > 0
        print(f"  {r['signal']:10} hold{r['hold']:3} n{r['size']:3} "
              f"ret={v['return_pct']:7.1f}% vs SPY {bv['return_pct']:.1f}% "
              f"dd={v['max_dd']:6.1%} CI[{lo:6.2f},{hi:6.2f}] "
              f"-> {'FINDING' if ok else 'fails validation'}")

    out = DATA / "stock_study.json"
    slim = []
    for r in rows:
        e = {k: v for k, v in r.items()}
        for key in ("train_pess", "train_base", "validate_pess"):
            if e.get(key):
                e[key] = {k: v for k, v in e[key].items() if k != "positions"}
        slim.append(e)
    out.write_text(json.dumps({"generated": datetime.now().isoformat(),
                               "bench_train": bt, "bench_validate": bv,
                               "configs": slim}, indent=1, default=str),
                   encoding="utf-8")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
