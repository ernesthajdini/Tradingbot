"""
AMENDMENT 11 — long volatility on index ETFs, gated by IV rank.

The spread-tax measurement retired single-name options: 13.9% of premium per
round trip against a variance risk premium worth 3-5% a month. Index options
cost 5.0%, and a BOUGHT index option needs only its premium rather than the
~$8,000 of collateral that made index spread selling unaffordable.

Amendment 9 fixed the bar a directional signal must clear at ~2.5-3%/month,
and nothing in this project reaches it. So this stops betting on direction
and buys VOLATILITY — the one thing an option expresses that a stock cannot.

    python csp_screener/backtest/longvol_study.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd

from csp_screener.backtest.condor_study import STORE as ISTORE
from csp_screener.backtest.day_store import DayStore
from csp_screener.backtest.longput_study import DATA, boot_ci

TRAIN = (date(2017, 1, 3), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))

REGIMES = ["iv_bottom20", "iv_bottom40", "none"]     # "none" = CONTROL
STRUCTURES = ["straddle", "put30", "call30"]
EXITS = ["dte21", "expiry"]

DTE_LO, DTE_HI = 20, 60
EXIT_DTE = 21
COMMISSION = 1.00
SLIP = {"base": 0.05, "pess": 0.10}
MIN_OI = 100
MAX_PREMIUM = 600.0
IV_LOOKBACK = 252
COOLDOWN_DAYS = 30


def atm_iv(rows, t):
    """The ticker's at-the-money implied vol on this session."""
    c = rows[(rows["ticker"] == t) & rows["iv"].notna() & rows["delta"].notna()]
    if c.empty:
        return None
    near = c.iloc[(c["delta"].abs() - 0.50).abs().argsort()[:6]]
    return float(near["iv"].median())


def pick_leg(rows, t, asof, right, target):
    """Quoted contract nearest the target |delta| in the DTE window."""
    c = rows[(rows["ticker"] == t) & (rows["right"] == right)]
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
    return c.assign(d=(c["delta"].abs() - target).abs()).nsmallest(
        1, "d").iloc[0]


def legs_for(rows, t, asof, structure):
    """[(right, expiration, strike, ask), ...] or None."""
    if structure == "straddle":
        p = pick_leg(rows, t, asof, "P", 0.50)
        if p is None:
            return None
        c = rows[(rows["ticker"] == t) & (rows["right"] == "C")
                 & (rows["expiration"] == p["expiration"])
                 & (rows["strike"] == p["strike"])
                 & (rows["bid"] > 0) & (rows["ask"] > 0)
                 & (rows["open_interest"] >= MIN_OI)]
        if c.empty:
            return None
        c = c.iloc[0]
        return [("P", p["expiration"], float(p["strike"]), float(p["ask"])),
                ("C", c["expiration"], float(c["strike"]), float(c["ask"]))]
    right = "P" if structure == "put30" else "C"
    r = pick_leg(rows, t, asof, right, 0.30)
    if r is None:
        return None
    return [(right, r["expiration"], float(r["strike"]), float(r["ask"]))]


def mark(rows, t, right, exp, k):
    m = rows[(rows["ticker"] == t) & (rows["right"] == right)
             & (rows["expiration"] == exp) & (rows["strike"] == k)]
    if len(m) and float(m.iloc[0]["bid"]) > 0:
        return float(m.iloc[0]["bid"])
    return None


def run(regime, structure, exit_rule, store, ivhist, window, band="pess"):
    slip = SLIP[band]
    days = [d for d in store.dates if window[0] <= d <= window[1]]
    open_pos, closed, last_entry = [], [], {}
    hist = defaultdict(lambda: deque(maxlen=IV_LOOKBACK))

    for d in days:
        rows = store.day(d)

        # ---- manage open positions on this session's real quotes
        still = []
        for p in open_pos:
            dte = (p["exp"] - d).days
            due = dte <= 0 or (exit_rule == "dte21" and dte <= EXIT_DTE)
            if not due:
                still.append(p)
                continue
            u = rows[rows["ticker"] == p["t"]]["underlying_price"]
            spot = float(u.iloc[0]) if len(u) else p["spot0"]
            exit_prem = 0.0
            for right, exp, k, _ in p["legs"]:
                px = mark(rows, p["t"], right, exp, k)
                if px is None:      # unquotable: intrinsic, no time value credited
                    px = max(k - spot, 0.0) if right == "P" else max(spot - k, 0.0)
                exit_prem += px
            n_legs = len(p["legs"])
            cost = 2 * n_legs * COMMISSION + slip * (p["entry"] + exit_prem) * 100
            closed.append({"entry_date": p["d0"], "ticker": p["t"],
                           "pnl": (exit_prem - p["entry"]) * 100 - cost,
                           "entry_px": p["entry"], "held": (d - p["d0"]).days})
        open_pos = still

        # ---- IV rank, then weekly entries
        ranks = {}
        for t in rows["ticker"].unique():
            iv = atm_iv(rows, t)
            if iv is None:
                continue
            h = hist[t]
            if len(h) >= IV_LOOKBACK // 2:
                ranks[t] = float(np.mean(np.asarray(h) < iv))
            h.append(iv)

        if d.weekday() != 0:
            continue
        for t, rk in sorted(ranks.items()):
            if regime == "iv_bottom20" and rk > 0.20:
                continue
            if regime == "iv_bottom40" and rk > 0.40:
                continue
            if last_entry.get(t) and (d - last_entry[t]).days < COOLDOWN_DAYS:
                continue
            legs = legs_for(rows, t, d, structure)
            if not legs:
                continue
            entry = sum(x[3] for x in legs)
            if entry <= 0 or entry * 100 > MAX_PREMIUM:
                continue
            u = rows[rows["ticker"] == t]["underlying_price"]
            open_pos.append({"t": t, "legs": legs, "entry": entry, "d0": d,
                             "exp": legs[0][1],
                             "spot0": float(u.iloc[0]) if len(u) else 0.0})
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


def show(tag, r):
    if not r:
        print(f"  {tag:30} no trades")
        return
    lo, hi = boot_ci(r["pnls"])
    print(f"  {tag:30} n={r['n']:4}({r['n_dates']:3}d) win={r['win']:4.0%} "
          f"paid=${r['avg_entry_cost']:5.0f} per-trade=${r['mean']:8.2f} "
          f"CI[{lo:8.2f},{hi:7.2f}] med=${r['median']:7.2f}", flush=True)


def main() -> int:
    t0 = time.time()
    store = DayStore(ISTORE)
    print(f"index store: {len(store.dates)} days {store.dates[0]} -> "
          f"{store.dates[-1]}")

    # ---- knob-bite proof BEFORE any result is read
    w = (date(2018, 1, 1), date(2019, 12, 31))
    a = run("iv_bottom20", "straddle", "dte21", store, None, w)
    b = run("none", "straddle", "dte21", store, None, w)
    bites = a and b and (a["n"], round(a["mean"], 4)) != (b["n"],
                                                         round(b["mean"], 4))
    print(f"KNOB PROOF regime: bottom20 n={a['n'] if a else 0} vs "
          f"control n={b['n'] if b else 0} -> "
          f"{'BITES' if bites else 'INERT — ABORT'}")
    if not bites:
        return 1

    print(f"\n=== TRAIN {TRAIN[0]}..{TRAIN[1]} (search, not evidence) ===")
    results = []
    for rg in REGIMES:
        for st in STRUCTURES:
            for ex in EXITS:
                r = run(rg, st, ex, store, None, TRAIN, "pess")
                results.append({"regime": rg, "structure": st, "exit": ex,
                                "train": r})
                show(f"{rg} {st} {ex}", r)

    ctrl = {(x["structure"], x["exit"]): x["train"]
            for x in results if x["regime"] == "none"}
    promoted = []
    for x in results:
        r = x["train"]
        if x["regime"] == "none" or not r:
            continue
        c = ctrl.get((x["structure"], x["exit"]))
        if (r["n"] >= 100 and r["mean"] > 0 and r["median"] > 0
                and (np.isnan(r["top_share"]) or r["top_share"] <= 0.40)
                and c and r["mean"] > c["mean"]):
            promoted.append(x)
    print(f"\nTRAIN: {len(results)} configs, {len(promoted)} met the "
          f"pre-registered bar")

    print(f"\n=== VALIDATE {VALID[0]}..{VALID[1]} ===")
    if not promoted:
        print("  none promoted — the validation window stays shut")
    for x in promoted:
        v = run(x["regime"], x["structure"], x["exit"], store, None, VALID,
                "pess")
        cv = run("none", x["structure"], x["exit"], store, None, VALID, "pess")
        x["validate"] = v
        show(f"{x['regime']} {x['structure']} {x['exit']}", v)
        if v and cv:
            lo, _ = boot_ci(v["pnls"])
            print(f"    control ${cv['mean']:.2f} -> "
                  f"{'HOLDS' if lo > 0 and v['mean'] > cv['mean'] else 'fails'}")

    out = DATA / "longvol_study.json"
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
