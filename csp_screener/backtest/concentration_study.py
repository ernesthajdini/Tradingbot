"""
AMENDMENT 8 — concentration + pattern signals.

Two mechanical levers Amendment 7 exposed and I closed too early:

  1. IBKR's $1.00 minimum per stock order is FIXED, so the percentage cost
     collapses with concentration: ~1.7% round trip at 10 names, ~0.83% at
     5, ~0.50% at 3, ~0.33% at 2. Every earlier study used 5 or 10.
  2. The 52-week-high underperformance (-104bp train, -150bp validate) needs
     no shorting to be useful — as an EXCLUSION filter it is free.

The "none" arm is a CONTROL: randomly chosen eligible names at the same
concentration and cost. If a signal cannot beat its own control, the result
is concentration luck, not information.

    python csp_screener/backtest/concentration_study.py
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

from csp_screener.backtest.pattern_study import load_ohlc, MIN_PRICE, MIN_VOL

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
TRAIN = (date(2017, 2, 8), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))
SIGNALS = ["gap_up", "gap_up_not_high", "breakout_not_high", "none"]
SIZES = [2, 3, 5]
HOLDS = [21, 63]
COMMISSION = 1.0
SLIP = {"base": 0.0010, "pess": 0.0025}
EQUITY = 1200.0
MIN_VIABLE_STAKE = 50.0


def build_panels():
    """close / adj / volume / signal-flag panels over every history."""
    C, A, V, G, B, H = {}, {}, {}, {}, {}, {}
    files = sorted((DATA / "stocks").glob("*.csv"))
    for i, p in enumerate(files, 1):
        try:
            df = load_ohlc(p)
        except Exception:
            continue
        if len(df) < 300:
            continue
        c, h, l, o, v = df["c"], df["h"], df["l"], df["o"], df["v"]
        C[p.stem] = c
        A[p.stem] = c
        V[p.stem] = v
        G[p.stem] = (o > c.shift(1) * 1.03)                      # gap up 3%
        B[p.stem] = (c > h.shift(1).rolling(20).max())           # 20d breakout
        H[p.stem] = (c > h.shift(1).rolling(252).max())          # 52w high
        if i % 3000 == 0:
            print(f"  loaded {i}/{len(files)}", flush=True)
    idx = pd.DataFrame(C).sort_index().index
    def frame(d, fill=np.nan):
        return pd.DataFrame(d).reindex(idx).sort_index()
    return (frame(C), frame(V), frame(G).fillna(False).astype(bool),
            frame(B).fillna(False).astype(bool),
            frame(H).fillna(False).astype(bool))


def run(signal, size, hold, C, V, G, B, H, window, band="pess", seed=5):
    slip = SLIP[band]
    idx = [d for d in C.index if window[0] <= d.date() <= window[1]]
    if len(idx) < hold + 1:
        return None
    rebal = idx[::hold]
    rng = np.random.default_rng(seed)
    equity, positions, curve = EQUITY, [], []
    blown = False

    for i, d in enumerate(rebal[:-1]):
        nxt = rebal[i + 1]
        px = C.loc[d]
        v20 = V.loc[:d].tail(20).mean()
        elig = px.notna() & (px >= MIN_PRICE) & (v20 >= MIN_VOL)
        if signal == "gap_up":
            mask = elig & G.loc[d]
        elif signal == "gap_up_not_high":
            mask = elig & G.loc[d] & ~H.loc[d]
        elif signal == "breakout_not_high":
            mask = elig & B.loc[d] & ~H.loc[d]
        else:
            mask = elig
        names = list(px[mask].index)
        if len(names) < size or equity < size * MIN_VIABLE_STAKE:
            if equity < size * MIN_VIABLE_STAKE:
                blown = True
                curve.append((d.date(), equity))
                break
            curve.append((d.date(), equity))
            continue
        # No ranking inside the signal: take a random draw of qualifying
        # names so the test measures the SIGNAL, not a second hidden sort.
        picks = list(rng.choice(names, size=size, replace=False))
        stake = equity / size
        period = 0.0
        for t in picks:
            p0 = C.at[d, t]
            if not np.isfinite(p0) or p0 <= 0:
                continue
            fut = C.loc[nxt:, t].dropna()
            if fut.empty:
                tail = C.loc[d:, t].dropna()
                if len(tail) < 2:
                    continue
                p1 = float(tail.iloc[-1])
            else:
                p1 = float(fut.iloc[0])
            sh = stake / p0
            pnl = sh * (p1 - p0) - (2 * COMMISSION + slip * (sh * p0 + sh * p1))
            positions.append(pnl)
            period += pnl
        equity += period
        curve.append((d.date(), equity))

    if not positions:
        return None
    a = np.array(positions)
    eq = np.array([c[1] for c in curve])
    peak = np.maximum.accumulate(eq)
    return {"n": len(a), "total": float(a.sum()),
            "return_pct": float(100 * (equity - EQUITY) / EQUITY),
            "win": float((a > 0).mean()),
            "max_dd": float(((eq - peak) / peak).min()),
            "final": float(equity), "blown": blown,
            "cost_pct_round_trip": round(100 * (2 * COMMISSION /
                                                (EQUITY / size) + 2 * slip), 2)}


def spy(C, window):
    s = C["SPY"].dropna()
    s = s[(s.index.date >= window[0]) & (s.index.date <= window[1])]
    eq = (EQUITY * (s / s.iloc[0])).to_numpy()
    peak = np.maximum.accumulate(eq)
    return {"return_pct": float(100 * (s.iloc[-1] / s.iloc[0] - 1)),
            "max_dd": float(((eq - peak) / peak).min())}


def main() -> int:
    t0 = time.time()
    print("building panels...", flush=True)
    C, V, G, B, H = build_panels()
    print(f"{C.shape[1]} tickers x {C.shape[0]} sessions "
          f"({(time.time()-t0)/60:.1f}min)")
    bt, bv = spy(C, TRAIN), spy(C, VALID)
    print(f"SPY train {bt['return_pct']:+.1f}% (dd {bt['max_dd']:.1%}) | "
          f"validate {bv['return_pct']:+.1f}% (dd {bv['max_dd']:.1%})\n")

    rows = []
    print(f"{'signal':20}{'n':>3}{'hold':>6}{'cost%':>7}{'pos':>6}"
          f"{'return':>10}{'maxDD':>9}  vs SPY")
    for sig in SIGNALS:
        for size in SIZES:
            for hold in HOLDS:
                r = run(sig, size, hold, C, V, G, B, H, TRAIN, "pess")
                if not r:
                    continue
                rows.append({"signal": sig, "size": size, "hold": hold,
                             "train": r})
                print(f"{sig:20}{size:3}{hold:6}{r['cost_pct_round_trip']:7.2f}"
                      f"{r['n']:6}{r['return_pct']:9.1f}%{r['max_dd']:8.1%}  "
                      f"{'BEATS' if r['return_pct'] > bt['return_pct'] else 'below'}"
                      f"{' BLOWN' if r['blown'] else ''}", flush=True)

    # promotion: beats SPY, drawdown bounded, AND beats its matched control
    ctrl = {(r["size"], r["hold"]): r["train"]["return_pct"]
            for r in rows if r["signal"] == "none"}
    promoted = [r for r in rows
                if r["signal"] != "none" and not r["train"]["blown"]
                and r["train"]["n"] >= 100
                and r["train"]["total"] > 0
                and r["train"]["return_pct"] > bt["return_pct"]
                and abs(r["train"]["max_dd"]) <= 1.5 * abs(bt["max_dd"])
                and r["train"]["return_pct"] > ctrl.get(
                    (r["size"], r["hold"]), 1e9)]
    print(f"\nTRAIN: {len(rows)} configs, {len(promoted)} promoted "
          f"(beat SPY {bt['return_pct']:.0f}%, dd <= {1.5*abs(bt['max_dd']):.0%}, "
          f"and beat their own control)")

    print(f"\n=== VALIDATION 2022-2023 (SPY {bv['return_pct']:+.1f}%) ===")
    if not promoted:
        print("  none — nothing met the pre-registered bar")
    for r in promoted:
        v = run(r["signal"], r["size"], r["hold"], C, V, G, B, H, VALID, "pess")
        cv = run("none", r["size"], r["hold"], C, V, G, B, H, VALID, "pess")
        r["validate"] = v
        if not v:
            print(f"  {r['signal']} n{r['size']} hold{r['hold']}: no positions")
            continue
        ok = (v["return_pct"] > bv["return_pct"] and v["total"] > 0
              and cv and v["return_pct"] > cv["return_pct"])
        print(f"  {r['signal']:20}n{r['size']} hold{r['hold']:3} "
              f"ret={v['return_pct']:+8.1f}% dd={v['max_dd']:7.1%} "
              f"control={cv['return_pct'] if cv else float('nan'):+8.1f}% "
              f"-> {'HOLDS' if ok else 'fails'}")

    out = DATA / "concentration_study.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(),
                               "spy_train": bt, "spy_validate": bv,
                               "configs": rows}, indent=1, default=str),
                   encoding="utf-8")
    print(f"\n({(time.time()-t0)/60:.1f}min) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
