"""
AMENDMENT 9 — long puts on the one signal that survived validation.

Across 306 configurations exactly one effect held out of sample: stocks at a
52-week high underperform the universe by ~104bp (train) / ~150bp (validate)
over the next 21 sessions. It was set aside because monetising a negative
view needs shorting. A LONG PUT does not.

This asks the only question that matters: is 130bp/month of drift bigger than
what a bought put pays away in time value?

Mechanics, all conservative:
  * enter at the ASK, exit at the BID (the spread is paid, not assumed away)
  * $1.00/contract/leg commission + 5%/10% slippage on premium both ways
  * a contract must have a two-sided quote AND open interest to be entered
  * exits mark on the real quote of the SAME contract on the exit date; if
    that contract is unquotable the position settles at intrinsic, which for
    a LONG put is the conservative direction (no time value to the holder)

    python csp_screener/backtest/longput_study.py
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

from csp_screener.backtest.day_store import DayStore, STORE
from csp_screener.backtest.pattern_study import load_ohlc, MIN_PRICE, MIN_VOL

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
TRAIN = (date(2017, 2, 8), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))

SIGNALS = ["at_52w_high", "high_and_rsi70", "none"]      # "none" = CONTROL
DELTAS = [0.30, 0.40]
EXITS = ["dte21", "tp100", "expiry"]

DTE_LO, DTE_HI = 20, 60   # Amendment 9A: monthlies only
COMMISSION = 1.00
SLIP = {"base": 0.05, "pess": 0.10}
MIN_OI = 100                 # long puts are cheap; production's 500 is for sold
MAX_PER_DAY = 3              # stops one date dominating the sample
EXIT_DTE = 21
MAX_PREMIUM = 600.0          # a $1,200 account cannot pay more for one contract


# ---------------------------------------------------------------------------
# signal panel
# ---------------------------------------------------------------------------

def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def build_signals(tickers):
    """{ticker: DataFrame[high52, rsi70, elig]} indexed by date."""
    out = {}
    for i, t in enumerate(sorted(tickers), 1):
        p = DATA / "stocks" / f"{t}.csv"
        if not p.exists():
            continue
        try:
            df = load_ohlc(p)
        except Exception:
            continue
        if len(df) < 300:
            continue
        c, h, v = df["c"], df["h"], df["v"]
        panel = pd.DataFrame({
            "high52": (c > h.shift(1).rolling(252).max()).fillna(False),
            "rsi70": (rsi(c) > 70).fillna(False),
            "elig": ((c >= MIN_PRICE) &
                     (v.shift(1).rolling(20).mean() >= MIN_VOL)).fillna(False),
        })
        panel.index = df.index.date
        out[t] = panel[~panel.index.duplicated(keep="last")]
        if i % 300 == 0:
            print(f"  signals {i}/{len(tickers)}", flush=True)
    return out


def flags(sig, t, d):
    """(eligible, at_52w_high, rsi>70) for ticker t on date d, or None."""
    s = sig.get(t)
    if s is None:
        return None
    try:
        row = s.loc[d]
    except KeyError:
        return None
    return bool(row["elig"]), bool(row["high52"]), bool(row["rsi70"])


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def pick_put(rows, t, asof, target):
    """The quoted put nearest the target delta inside the DTE window."""
    c = rows[rows["ticker"] == t]
    if c.empty:
        return None
    dte = np.array([(e - asof).days for e in c["expiration"]])
    c = c[(dte >= DTE_LO) & (dte <= DTE_HI)]
    if c.empty:
        return None
    c = c[(c["bid"] > 0) & (c["ask"] > 0) & (c["open_interest"] >= MIN_OI)
          & c["delta"].notna()]
    if c.empty:
        return None
    return c.assign(dist=(c["delta"].abs() - target).abs()).nsmallest(
        1, "dist").iloc[0]


def run(signal, target, exit_rule, store, sig, window, band="pess", seed=11):
    slip = SLIP[band]
    rng = np.random.default_rng(seed)
    days = [d for d in store.dates if window[0] <= d <= window[1]]
    open_pos, closed, last_entry = [], [], {}

    for d in days:
        rows = store.day(d)

        # ---- manage open positions on this day's real quotes
        still = []
        for p in open_pos:
            dte = (p["exp"] - d).days
            m = rows[(rows["ticker"] == p["t"]) &
                     (rows["expiration"] == p["exp"]) &
                     (rows["strike"] == p["k"])]
            px = float(m.iloc[0]["bid"]) if len(m) and float(
                m.iloc[0]["bid"]) > 0 else None
            due = dte <= 0 or (exit_rule in ("dte21", "tp100")
                               and dte <= EXIT_DTE)
            if exit_rule == "tp100" and px is not None and px >= 2 * p["entry"]:
                due = True
            if not due:
                still.append(p)
                continue
            if px is None:
                u = rows[rows["ticker"] == p["t"]]["underlying_price"]
                spot = float(u.iloc[0]) if len(u) else p["spot0"]
                px = max(p["k"] - spot, 0.0)
            cost = 2 * COMMISSION + slip * (p["entry"] + px) * 100
            closed.append({"entry_date": p["d0"], "ticker": p["t"],
                           "pnl": (px - p["entry"]) * 100 - cost,
                           "entry_px": p["entry"], "held": (d - p["d0"]).days})
        open_pos = still

        cands = []
        for t in rows["ticker"].unique():
            f = flags(sig, t, d)
            if not f or not f[0]:
                continue
            _, hi, r70 = f
            if signal == "at_52w_high" and not hi:
                continue
            if signal == "high_and_rsi70" and not (hi and r70):
                continue
            if last_entry.get(t) and (d - last_entry[t]).days < 30:
                continue
            cands.append(t)
        if not cands:
            continue
        if len(cands) > MAX_PER_DAY:
            cands = list(rng.choice(cands, MAX_PER_DAY, replace=False))
        for t in cands:
            row = pick_put(rows, t, d, target)
            if row is None:
                continue
            ask = float(row["ask"])
            if ask <= 0 or ask * 100 > MAX_PREMIUM:
                continue
            open_pos.append({"t": t, "exp": row["expiration"],
                             "k": float(row["strike"]), "entry": ask, "d0": d,
                             "spot0": float(row["underlying_price"])})
            last_entry[t] = d

    if not closed:
        return None
    df = pd.DataFrame(closed)
    a = df["pnl"].to_numpy()
    by_date = df.groupby("entry_date")["pnl"].mean().to_numpy()
    return {
        "n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
        "total": float(a.sum()), "win": float((a > 0).mean()),
        "n_dates": int(df["entry_date"].nunique()),
        "se_clustered": (float(by_date.std(ddof=1) / np.sqrt(len(by_date)))
                         if len(by_date) > 1 else float("nan")),
        "top_share": (float(a.max() / a.sum()) if a.sum() > 0
                      else float("nan")),
        "avg_entry_cost": float(df["entry_px"].mean() * 100),
        "avg_held": float(df["held"].mean()),
        "pnls": a.tolist(),
    }


def boot_ci(v, iters=10000, seed=17):
    a = np.asarray(v, dtype=float)
    rng = np.random.default_rng(seed)
    m = rng.choice(a, size=(iters, len(a)), replace=True).mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def show(tag, r):
    if not r:
        print(f"  {tag:32} no trades")
        return
    lo, hi = boot_ci(r["pnls"])
    print(f"  {tag:32} n={r['n']:4}({r['n_dates']:3}d) win={r['win']:4.0%} "
          f"paid=${r['avg_entry_cost']:5.0f} per-trade=${r['mean']:8.2f} "
          f"CI[{lo:8.2f},{hi:7.2f}] med=${r['median']:7.2f}", flush=True)


def main() -> int:
    t0 = time.time()
    store = DayStore(STORE)
    print(f"store: {len(store.dates)} days {store.dates[0]} -> {store.dates[-1]}")

    print("collecting option universe...", flush=True)
    tk = set()
    for d in [x for x in store.dates if TRAIN[0] <= x <= VALID[1]][::40]:
        tk |= set(store.day(d)["ticker"].unique())
    print(f"  {len(tk)} tickers; building signal panels...", flush=True)
    sig = build_signals(tk)
    print(f"  {len(sig)} panels ({(time.time()-t0)/60:.1f}min)\n")

    print(f"=== TRAIN {TRAIN[0]}..{TRAIN[1]} (search, not evidence) ===")
    results = []
    for s in SIGNALS:
        for dlt in DELTAS:
            for ex in EXITS:
                r = run(s, dlt, ex, store, sig, TRAIN, "pess")
                results.append({"signal": s, "delta": dlt, "exit": ex,
                                "train": r})
                show(f"{s} d{dlt} {ex}", r)

    ctrl = {(x["delta"], x["exit"]): x["train"]
            for x in results if x["signal"] == "none"}
    promoted = []
    for x in results:
        r = x["train"]
        if x["signal"] == "none" or not r:
            continue
        c = ctrl.get((x["delta"], x["exit"]))
        if (r["n"] >= 100 and r["mean"] > 0 and r["median"] > 0
                and (np.isnan(r["top_share"]) or r["top_share"] <= 0.40)
                and c and r["mean"] > c["mean"]):
            promoted.append(x)
    print(f"\nTRAIN: {len(results)} configs, {len(promoted)} met the "
          f"pre-registered bar\n  (n>=100, mean>0 pessimistic, median>0, "
          f"no trade >40% of P&L, beats its own control)")

    print(f"\n=== VALIDATE {VALID[0]}..{VALID[1]} ===")
    if not promoted:
        print("  none promoted — the validation window stays shut")
    for x in promoted:
        v = run(x["signal"], x["delta"], x["exit"], store, sig, VALID, "pess")
        cv = run("none", x["delta"], x["exit"], store, sig, VALID, "pess")
        x["validate"] = v
        show(f"{x['signal']} d{x['delta']} {x['exit']}", v)
        if v and cv:
            lo, _ = boot_ci(v["pnls"])
            print(f"    control ${cv['mean']:.2f} -> "
                  f"{'HOLDS' if lo > 0 and v['mean'] > cv['mean'] else 'fails'}")

    out = DATA / "longput_study.json"
    out.write_text(json.dumps(
        {"generated": datetime.now().isoformat(),
         "results": [{k: ({kk: vv for kk, vv in val.items() if kk != "pnls"}
                          if isinstance(val, dict) else val)
                      for k, val in x.items()} for x in results]},
        indent=1, default=str), encoding="utf-8")
    print(f"\n({(time.time()-t0)/60:.1f}min) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
