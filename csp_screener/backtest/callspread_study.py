"""
AMENDMENT 12 — short call spreads on 52-week-high names.

The only structure where the project's two measured facts point the same
way: option buyers overpay (long vol loses gross of friction), AND names at a
52-week high underperform for 21 days (validated, p=7e-19). Every earlier
selling test was bullish or neutral; every buying test paid the premium.

Defined risk: sell a call near the target delta, buy one $2-5 higher. A $5
spread needs ~$500 of collateral, not the ~$8,000 that made index spread
selling impossible.

Mechanics, all conservative:
  * credit  = short BID - long ASK  (the spread is paid on both legs)
  * buyback = short ASK - long BID
  * 4 legs round trip x $1.00 + 5%/10% slippage on credit and buyback
  * both legs two-sided; short leg OI >= 500 (production's floor for sold
    contracts) — the study REFUSES to run if the store carries no OI
  * never sell in the money; exits mark on real quotes, intrinsic otherwise

    python csp_screener/backtest/callspread_study.py
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

from csp_screener.backtest.build_call_store import CALL_STORE
from csp_screener.backtest.day_store import DayStore
from csp_screener.backtest.longput_study import (COMMISSION, DATA, SLIP, TRAIN,
                                                 VALID, boot_ci, build_signals,
                                                 flags)

SIGNALS = ["at_52w_high", "none"]          # "none" = CONTROL
DELTAS = [0.25, 0.30]
WIDTHS = [2.0, 5.0]
EXITS = ["dte21", "tp50"]

DTE_LO, DTE_HI = 25, 45
EXIT_DTE = 21
MIN_OI_SHORT = 500
MIN_OI_LONG = 100
MAX_PER_DAY = 3
MAX_COLLATERAL = 600.0        # width x 100 must fit a $1,200 account
MIN_CREDIT = 0.10             # below this the spread is dust


def pick_spread(rows, t, asof, target, width):
    c = rows[rows["ticker"] == t]
    if c.empty:
        return None
    dte = np.array([(e - asof).days for e in c["expiration"]])
    c = c[(dte >= DTE_LO) & (dte <= DTE_HI)]
    if c.empty:
        return None
    q = c[(c["bid"] > 0) & (c["ask"] > 0)]
    s = q[(q["open_interest"] >= MIN_OI_SHORT) & q["delta"].notna()
          & (q["strike"] > q["underlying_price"])]        # never sell ITM
    if s.empty:
        return None
    s = s.assign(d=(s["delta"] - target).abs()).nsmallest(1, "d").iloc[0]
    longs = q[(q["expiration"] == s["expiration"])
              & (q["strike"] >= s["strike"] + width)
              & (q["strike"] <= s["strike"] + 1.5 * width)
              & (q["open_interest"] >= MIN_OI_LONG)]
    if longs.empty:
        return None
    l = longs.nsmallest(1, "strike").iloc[0]
    credit = float(s["bid"]) - float(l["ask"])
    if credit < MIN_CREDIT:
        return None
    return {"exp": s["expiration"], "ks": float(s["strike"]),
            "kl": float(l["strike"]), "credit": credit,
            "width": float(l["strike"]) - float(s["strike"]),
            "spot0": float(s["underlying_price"])}


def mark(rows, t, exp, k):
    m = rows[(rows["ticker"] == t) & (rows["expiration"] == exp)
             & (rows["strike"] == k)]
    if len(m) and float(m.iloc[0]["bid"]) > 0 and float(m.iloc[0]["ask"]) > 0:
        return float(m.iloc[0]["bid"]), float(m.iloc[0]["ask"])
    return None


def buyback(rows, p):
    """Cost to close: short ASK - long BID, or intrinsic if unquotable."""
    s, l = mark(rows, p["t"], p["exp"], p["ks"]), mark(rows, p["t"], p["exp"], p["kl"])
    if s and l:
        return max(s[1] - l[0], 0.0)
    u = rows[rows["ticker"] == p["t"]]["underlying_price"]
    spot = float(u.iloc[0]) if len(u) else p["spot0"]
    return max(spot - p["ks"], 0.0) - max(spot - p["kl"], 0.0)


def run(signal, target, width, exit_rule, store, sig, window, band="pess",
        seed=11):
    slip = SLIP[band]
    rng = np.random.default_rng(seed)
    days = [d for d in store.dates if window[0] <= d <= window[1]]
    open_pos, closed, last_entry = [], [], {}

    for d in days:
        rows = store.day(d)

        still = []
        for p in open_pos:
            dte = (p["exp"] - d).days
            due = dte <= 0 or dte <= EXIT_DTE
            cost = None
            if exit_rule == "tp50" and not due:
                cost = buyback(rows, p)
                if cost <= 0.5 * p["credit"]:
                    due = True
            if not due:
                still.append(p)
                continue
            if cost is None:
                cost = buyback(rows, p)
            fric = 4 * COMMISSION + slip * (p["credit"] + cost) * 100
            closed.append({"entry_date": p["d0"], "ticker": p["t"],
                           "pnl": (p["credit"] - cost) * 100 - fric,
                           "credit": p["credit"], "width": p["width"],
                           "held": (d - p["d0"]).days})
        open_pos = still

        cands = []
        for t in rows["ticker"].unique():
            f = flags(sig, t, d)
            if not f or not f[0]:
                continue
            if signal == "at_52w_high" and not f[1]:
                continue
            if last_entry.get(t) and (d - last_entry[t]).days < 30:
                continue
            cands.append(t)
        if not cands:
            continue
        if len(cands) > MAX_PER_DAY:
            cands = list(rng.choice(sorted(cands), MAX_PER_DAY, replace=False))
        for t in cands:
            sp = pick_spread(rows, t, d, target, width)
            if sp is None or sp["width"] * 100 > MAX_COLLATERAL:
                continue
            sp.update({"t": t, "d0": d})
            open_pos.append(sp)
            last_entry[t] = d

    if not closed:
        return None
    df = pd.DataFrame(closed)
    a = df["pnl"].to_numpy()
    return {
        "n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
        "total": float(a.sum()), "win": float((a > 0).mean()),
        "n_dates": int(df["entry_date"].nunique()),
        "avg_credit": float(df["credit"].mean() * 100),
        "avg_width": float(df["width"].mean()),
        "top_share": (float(a.max() / a.sum()) if a.sum() > 0 else float("nan")),
        "pnls": a.tolist(),
    }


def show(tag, r):
    if not r:
        print(f"  {tag:34} no trades")
        return
    lo, hi = boot_ci(r["pnls"])
    print(f"  {tag:34} n={r['n']:4} win={r['win']:4.0%} "
          f"credit=${r['avg_credit']:5.0f} per-trade=${r['mean']:8.2f} "
          f"CI[{lo:8.2f},{hi:7.2f}] med=${r['median']:7.2f}", flush=True)


def main() -> int:
    t0 = time.time()
    store = DayStore(CALL_STORE)
    print(f"call store: {len(store.dates)} days {store.dates[0]} -> "
          f"{store.dates[-1]}")

    # The OI gate is a declared part of the design. Refuse to run without it
    # rather than silently downgrade the liquidity floor.
    probe = pd.concat([store.day(d) for d in store.dates[::200]])
    if not (probe["open_interest"] > 0).any():
        print("REFUSING TO RUN: call store carries no open interest — run "
              "calls_oi_pull.py and rebuild the store first.")
        return 1

    tk = set()
    for d in [x for x in store.dates if TRAIN[0] <= x <= VALID[1]][::40]:
        tk |= set(store.day(d)["ticker"].unique())
    sig = build_signals(tk)
    print(f"{len(sig)} signal panels ({(time.time()-t0)/60:.1f}min)\n")

    w = (date(2018, 1, 1), date(2018, 12, 31))
    a = run("at_52w_high", 0.30, 5.0, "dte21", store, sig, w)
    b = run("none", 0.30, 5.0, "dte21", store, sig, w)
    bites = a and b and (a["n"], round(a["mean"], 4)) != (b["n"], round(b["mean"], 4))
    print(f"KNOB PROOF signal: n={a['n'] if a else 0} vs control "
          f"n={b['n'] if b else 0} -> {'BITES' if bites else 'INERT — ABORT'}")
    if not bites:
        return 1

    print(f"\n=== TRAIN {TRAIN[0]}..{TRAIN[1]} (search, not evidence) ===")
    results = []
    for s in SIGNALS:
        for dlt in DELTAS:
            for wd in WIDTHS:
                for ex in EXITS:
                    r = run(s, dlt, wd, ex, store, sig, TRAIN, "pess")
                    results.append({"signal": s, "delta": dlt, "width": wd,
                                    "exit": ex, "train": r})
                    show(f"{s} d{dlt} w{wd:.0f} {ex}", r)

    ctrl = {(x["delta"], x["width"], x["exit"]): x["train"]
            for x in results if x["signal"] == "none"}
    promoted = []
    for x in results:
        r = x["train"]
        if x["signal"] == "none" or not r:
            continue
        c = ctrl.get((x["delta"], x["width"], x["exit"]))
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
        v = run(x["signal"], x["delta"], x["width"], x["exit"], store, sig,
                VALID, "pess")
        cv = run("none", x["delta"], x["width"], x["exit"], store, sig,
                 VALID, "pess")
        x["validate"] = v
        show(f"{x['signal']} d{x['delta']} w{x['width']:.0f} {x['exit']}", v)
        if v and cv:
            lo, _ = boot_ci(v["pnls"])
            print(f"    control ${cv['mean']:.2f} -> "
                  f"{'HOLDS' if lo > 0 and v['mean'] > cv['mean'] else 'fails'}")

    out = DATA / "callspread_study.json"
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
