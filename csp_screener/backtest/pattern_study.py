"""
AMENDMENT 7 — can a chart be read?

Separates two questions every prior study conflated:

  1. STATISTICAL — does a pattern's occurrence shift forward returns at all,
     versus the same universe on every other day?
  2. ECONOMIC — is that shift bigger than the ~0.83% round trip a $240
     position pays at this account size?

A pattern can pass (1) and fail (2), and knowing which is the answer to
"is there information in the chart, or not?"

Sample sizes here are enormous (~9,000 names x 5 years), so trivial effects
WILL clear any p-value. The report therefore leads with effect size in basis
points and shows the cost line next to it.

    python csp_screener/backtest/pattern_study.py
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

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
TRAIN = (date(2017, 2, 8), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))
HORIZONS = [1, 5, 21]
MIN_PRICE, MIN_VOL = 5.0, 1_000_000
ROUND_TRIP_PCT = 0.83          # measured: $2 commission + slippage on ~$240
ALPHA = 0.05 / 36


def load_ohlc(path):
    df = pd.read_csv(path, usecols=["created", "open", "high", "low",
                                    "close", "volume"])
    idx = pd.DatetimeIndex(pd.to_datetime(df["created"]).dt.normalize())
    out = pd.DataFrame({
        "o": pd.to_numeric(df["open"], errors="coerce").to_numpy(),
        "h": pd.to_numeric(df["high"], errors="coerce").to_numpy(),
        "l": pd.to_numeric(df["low"], errors="coerce").to_numpy(),
        "c": pd.to_numeric(df["close"], errors="coerce").to_numpy(),
        "v": pd.to_numeric(df["volume"], errors="coerce").fillna(0).to_numpy(),
    }, index=idx)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out[(out["c"] > 0) & (out["h"] > 0) & (out["l"] > 0)]


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def patterns(df):
    """Boolean series per declared pattern. Every value uses only data up to
    and including that session — no look-ahead."""
    c, h, l, o, v = df["c"], df["h"], df["l"], df["o"], df["v"]
    prior_hi20 = h.shift(1).rolling(20).max()
    prior_lo20 = l.shift(1).rolling(20).min()
    prior_hi252 = h.shift(1).rolling(252).max()
    tr = pd.concat([h - l, (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    r = rsi(c)
    body = (c - o).abs()
    lower_wick = np.minimum(o, c) - l
    rng = (h - l).replace(0, np.nan)
    return {
        "breakout_20d": c > prior_hi20,
        "high_52w": c > prior_hi252,
        "breakdown_20d": c < prior_lo20,
        "gap_up_3pct": o > c.shift(1) * 1.03,
        "gap_down_3pct": o < c.shift(1) * 0.97,
        "inside_day": (h < h.shift(1)) & (l > l.shift(1)),
        "nr7": tr <= tr.shift(1).rolling(6).min(),
        "golden_cross": (ma50 > ma200) & (ma50.shift(1) <= ma200.shift(1)),
        "rsi_below_30": r < 30,
        "rsi_above_70": r > 70,
        "volume_spike_3x": v > 3 * v.shift(1).rolling(20).mean(),
        "hammer": (lower_wick >= 2 * body) & ((c - l) / rng >= 0.667),
    }


def main() -> int:
    t0 = time.time()
    files = sorted((DATA / "stocks").glob("*.csv"))
    print(f"scanning {len(files)} histories for 12 patterns x 3 horizons",
          flush=True)

    # pattern -> horizon -> {"sig": [...], "base": [...]} per window
    acc = {w: {} for w in ("train", "valid")}
    n_days = 0
    for i, p in enumerate(files, 1):
        try:
            df = load_ohlc(p)
        except Exception:
            continue
        if len(df) < 300:
            continue
        elig = (df["c"] >= MIN_PRICE) & (
            df["v"].shift(1).rolling(20).mean() >= MIN_VOL)
        if not elig.any():
            continue
        pats = patterns(df)
        fwd = {hz: df["c"].shift(-hz) / df["c"] - 1.0 for hz in HORIZONS}
        d = df.index.date
        for wname, (a, b) in (("train", TRAIN), ("valid", VALID)):
            win = elig & (d >= a) & (d <= b)
            if not win.any():
                continue
            for hz in HORIZONS:
                f = fwd[hz]
                base = f[win].dropna()
                acc[wname].setdefault(("__BASE__", hz), []).append(
                    base.to_numpy())
                for name, mask in pats.items():
                    sel = f[win & mask.fillna(False)].dropna()
                    if len(sel):
                        acc[wname].setdefault((name, hz), []).append(
                            sel.to_numpy())
        n_days += int(elig.sum())
        if i % 2000 == 0:
            print(f"  {i}/{len(files)} ({(time.time()-t0)/60:.1f}min)",
                  flush=True)

    print(f"\n{n_days:,} eligible stock-days scanned "
          f"({(time.time()-t0)/60:.1f}min)\n")

    rows = []
    for wname in ("train", "valid"):
        for hz in HORIZONS:
            base = np.concatenate(acc[wname][("__BASE__", hz)])
            bmean = float(base.mean())
            for name in patterns(load_ohlc(files[0])).keys():
                key = (name, hz)
                if key not in acc[wname]:
                    continue
                sig = np.concatenate(acc[wname][key])
                if len(sig) < 100:
                    continue
                t, p = stats.ttest_ind(sig, base, equal_var=False)
                edge_bp = 1e4 * (float(sig.mean()) - bmean)
                rows.append({
                    "window": wname, "pattern": name, "horizon": hz,
                    "n": int(len(sig)), "signal_mean_bp": 1e4 * float(sig.mean()),
                    "base_mean_bp": 1e4 * bmean, "edge_bp": edge_bp,
                    "t": float(t), "p": float(p),
                    "beats_costs": bool(abs(edge_bp) > ROUND_TRIP_PCT * 100),
                })

    tr = [r for r in rows if r["window"] == "train"]
    tr.sort(key=lambda r: -abs(r["edge_bp"]))
    print(f"=== TRAIN: strongest effects by SIZE (alpha={ALPHA:.5f}, "
          f"cost hurdle = {ROUND_TRIP_PCT*100:.0f} bp) ===")
    print(f"{'pattern':18}{'hz':>4}{'n':>9}{'edge bp':>10}{'p':>10}  verdict")
    for r in tr[:14]:
        sig = "significant" if r["p"] < ALPHA else "not sig"
        econ = "BEATS COSTS" if r["beats_costs"] else "below costs"
        print(f"{r['pattern']:18}{r['horizon']:4}{r['n']:9,}"
              f"{r['edge_bp']:10.1f}{r['p']:10.1e}  {sig}, {econ}")

    tradeable = [r for r in tr if r["p"] < ALPHA and r["beats_costs"]]
    print(f"\n{len([r for r in tr if r['p'] < ALPHA])} of {len(tr)} tests are "
          f"statistically significant; {len(tradeable)} clear the cost hurdle")

    if tradeable:
        print("\n=== VALIDATION of cost-clearing patterns (2022-2023) ===")
        for r in tradeable:
            v = next((x for x in rows if x["window"] == "valid"
                      and x["pattern"] == r["pattern"]
                      and x["horizon"] == r["horizon"]), None)
            if not v:
                print(f"  {r['pattern']:18} hz{r['horizon']}: no validation data")
                continue
            same_sign = np.sign(v["edge_bp"]) == np.sign(r["edge_bp"])
            ok = same_sign and v["p"] < ALPHA and v["beats_costs"]
            print(f"  {r['pattern']:18} hz{r['horizon']:3} "
                  f"train {r['edge_bp']:+8.1f}bp -> valid {v['edge_bp']:+8.1f}bp "
                  f"(p={v['p']:.1e}) -> {'HOLDS' if ok else 'fails'}")

    out = DATA / "pattern_study.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(),
                               "alpha": ALPHA, "cost_bp": ROUND_TRIP_PCT * 100,
                               "rows": rows}, indent=1), encoding="utf-8")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
