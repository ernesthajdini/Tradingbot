"""
AMENDMENT 14 — earnings IV crush, defined risk.

Every premium-selling test so far harvested the MONTHLY variance premium:
3-5% of premium against a 13.9% round-trip toll. The earnings crush is a
different prize — implied vol collapses 20-40% across the announcement in
about two sessions. This is the one selling context where the prize per
round trip plausibly exceeds the toll.

  earnings arm : enter at the close BEFORE the date, exit at the close AFTER
  control arm  : same ticker, same structure, entered 30 calendar days
                 earlier (mid-quarter), same 2-session hold

The control is the whole test. A 2-session short spread earns theta anywhere;
only the EXCESS over mid-quarter is the crush.

    python csp_screener/backtest/earnings_crush_study.py
"""

from __future__ import annotations

import bisect
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd

from csp_screener.backtest.day_store import DayStore, STORE
from csp_screener.backtest.longput_study import (COMMISSION, DATA, SLIP, TRAIN,
                                                 VALID, boot_ci, build_signals,
                                                 flags)
from csp_screener.backtest.tail_study import load_earnings

ARMS = ["earnings", "control"]
STRUCTURES = ["put_spread"]           # "condor" joins when the call store exists
DELTAS = [0.20, 0.30]
WIDTHS = [2.0, 5.0]

DTE_LO, DTE_HI = 7, 30
MIN_OI_SHORT = 500
MIN_OI_LONG = 100
MIN_CREDIT = 0.10
MAX_COLLATERAL = 600.0
MAX_PER_DAY = 5
CONTROL_OFFSET_DAYS = 30
CONTROL_MIN_GAP_DAYS = 10             # control entry must sit away from any earnings
FILL = "cross"                        # "mid" = diagnostic only, never promotable



def pick_put_spread(rows, t, asof, target, width):
    c = rows[rows["ticker"] == t]
    if c.empty:
        return None
    dte = np.array([(e - asof).days for e in c["expiration"]])
    c = c[(dte >= DTE_LO) & (dte <= DTE_HI)]
    if c.empty:
        return None
    nearest = min(c["expiration"].unique(), key=lambda e: (e - asof).days)
    q = c[(c["expiration"] == nearest) & (c["bid"] > 0) & (c["ask"] > 0)]
    s = q[(q["open_interest"] >= MIN_OI_SHORT) & q["delta"].notna()
          & (q["strike"] < q["underlying_price"])]
    if s.empty:
        return None
    s = s.assign(d=(s["delta"].abs() - target).abs()).nsmallest(1, "d").iloc[0]
    longs = q[(q["strike"] <= s["strike"] - width)
              & (q["strike"] >= s["strike"] - 1.5 * width)
              & (q["open_interest"] >= MIN_OI_LONG)]
    if longs.empty:
        return None
    l = longs.nlargest(1, "strike").iloc[0]
    credit = (float(s["bid"]) - float(l["ask"]) if FILL == "cross" else
              0.5 * (float(s["bid"]) + float(s["ask"])) - 0.5 * (float(l["bid"]) + float(l["ask"])))
    if credit < MIN_CREDIT:
        return None
    return {"exp": nearest, "legs": [("P", float(s["strike"]), -1),
                                     ("P", float(l["strike"]), +1)],
            "credit": credit, "width": float(s["strike"]) - float(l["strike"]),
            "spot0": float(s["underlying_price"])}


def pick_condor(rows, crows, t, asof, target, width):
    ps = pick_put_spread(rows, t, asof, target, width)
    if ps is None:
        return None
    c = crows[(crows["ticker"] == t) & (crows["expiration"] == ps["exp"])
              & (crows["bid"] > 0) & (crows["ask"] > 0)]
    s = c[(c["open_interest"] >= MIN_OI_SHORT) & c["delta"].notna()
          & (c["strike"] > c["underlying_price"])]
    if s.empty:
        return None
    s = s.assign(d=(s["delta"] - target).abs()).nsmallest(1, "d").iloc[0]
    longs = c[(c["strike"] >= s["strike"] + width)
              & (c["strike"] <= s["strike"] + 1.5 * width)
              & (c["open_interest"] >= MIN_OI_LONG)]
    if longs.empty:
        return None
    l = longs.nsmallest(1, "strike").iloc[0]
    cc = (float(s["bid"]) - float(l["ask"]) if FILL == "cross" else
          0.5 * (float(s["bid"]) + float(s["ask"])) - 0.5 * (float(l["bid"]) + float(l["ask"])))
    if cc < MIN_CREDIT:
        return None
    ps["legs"] += [("C", float(s["strike"]), -1), ("C", float(l["strike"]), +1)]
    ps["credit"] += cc
    ps["width"] = max(ps["width"], float(l["strike"]) - float(s["strike"]))
    return ps


def leg_quote(rows, t, exp, k):
    m = rows[(rows["ticker"] == t) & (rows["expiration"] == exp)
             & (rows["strike"] == k)]
    if len(m) and float(m.iloc[0]["bid"]) > 0 and float(m.iloc[0]["ask"]) > 0:
        b_, a_ = float(m.iloc[0]["bid"]), float(m.iloc[0]["ask"])
        if FILL == "mid":
            mid = 0.5 * (b_ + a_)
            return mid, mid
        return b_, a_
    return None


def buyback(rows, crows, p):
    """Cost to close every leg: pay ask on shorts, receive bid on longs;
    intrinsic where unquotable (conservative for the seller)."""
    u = rows[rows["ticker"] == p["t"]]["underlying_price"]
    spot = float(u.iloc[0]) if len(u) else p["spot0"]
    total = 0.0
    for right, k, sign in p["legs"]:
        src = rows if right == "P" else crows
        q = leg_quote(src, p["t"], p["exp"], k) if src is not None else None
        if q:
            px = q[1] if sign < 0 else q[0]
        else:
            px = max(k - spot, 0.0) if right == "P" else max(spot - k, 0.0)
        total += px if sign < 0 else -px
    return max(total, 0.0)


def run(arm, structure, target, width, store, cstore, sig, earn, window,
        band="pess", seed=11):
    slip = SLIP[band]
    rng = np.random.default_rng(seed)
    dates = [d for d in store.dates if window[0] - timedelta(days=45) <= d
             <= window[1] + timedelta(days=10)]
    in_window = lambda d: window[0] <= d <= window[1]

    # entry sessions: the store date strictly before each event (or 30 days
    # earlier for control), exit = the store date strictly after the event
    plan = defaultdict(list)
    for t, evs in earn.items():
        for e in evs:
            if not in_window(e):
                continue
            i = bisect.bisect_left(dates, e)
            if i == 0 or i >= len(dates):
                continue
            d0, d1 = dates[i - 1], dates[i] if dates[i] > e else (
                dates[i + 1] if i + 1 < len(dates) else None)
            if d1 is None:
                continue
            if arm == "control":
                c0 = d0 - timedelta(days=CONTROL_OFFSET_DAYS)
                j = bisect.bisect_left(dates, c0)
                if j >= len(dates) - 2:
                    continue
                d0c = dates[j]
                d1c = dates[j + 2]                   # same 2-session hold
                near = any(abs((x - d0c).days) < CONTROL_MIN_GAP_DAYS
                           or abs((x - d1c).days) < CONTROL_MIN_GAP_DAYS
                           for x in evs)
                if near:
                    continue
                d0, d1 = d0c, d1c
            plan[d0].append((t, d1))

    open_pos, closed = [], []
    n_legs = 2 if structure == "put_spread" else 4
    for d in dates:
        if not open_pos and d not in plan:
            continue
        rows = store.day(d)
        crows = cstore.day(d) if cstore is not None else None

        still = []
        for p in open_pos:
            if d < p["d1"]:
                still.append(p)
                continue
            cost = buyback(rows, crows, p)
            fric = 2 * n_legs * COMMISSION + slip * (p["credit"] + cost) * 100
            closed.append({"entry_date": p["d0"], "ticker": p["t"],
                           "pnl": (p["credit"] - cost) * 100 - fric,
                           "credit": p["credit"], "width": p["width"]})
        open_pos = still

        todays = plan.get(d, [])
        if not todays:
            continue
        elig = []
        for t, d1 in todays:
            f = flags(sig, t, d)
            if f and f[0]:
                elig.append((t, d1))
        if len(elig) > MAX_PER_DAY:
            idx = rng.choice(len(elig), MAX_PER_DAY, replace=False)
            elig = [elig[i] for i in idx]
        for t, d1 in elig:
            sp = (pick_put_spread(rows, t, d, target, width)
                  if structure == "put_spread"
                  else pick_condor(rows, crows, t, d, target, width))
            if sp is None or sp["width"] * 100 > MAX_COLLATERAL:
                continue
            sp.update({"t": t, "d0": d, "d1": d1})
            open_pos.append(sp)

    if not closed:
        return None
    df = pd.DataFrame(closed)
    a = df["pnl"].to_numpy()
    return {
        "n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
        "total": float(a.sum()), "win": float((a > 0).mean()),
        "n_dates": int(df["entry_date"].nunique()),
        "avg_credit": float(df["credit"].mean() * 100),
        "top_share": (float(a.max() / a.sum()) if a.sum() > 0 else float("nan")),
        "pnls": a.tolist(),
    }


def show(tag, r):
    if not r:
        print(f"  {tag:34} no trades")
        return
    lo, hi = boot_ci(r["pnls"])
    print(f"  {tag:34} n={r['n']:5} win={r['win']:4.0%} "
          f"credit=${r['avg_credit']:5.0f} per-trade=${r['mean']:8.2f} "
          f"CI[{lo:8.2f},{hi:7.2f}] med=${r['median']:7.2f}", flush=True)


def main() -> int:
    import argparse
    from csp_screener.backtest.build_call_store import CALL_STORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=str(STORE))
    ap.add_argument("--call-store", default=str(CALL_STORE))
    ap.add_argument("--tag", default="archive")
    a = ap.parse_args()
    t0 = time.time()
    store = DayStore(Path(a.store))
    cstore = None
    cs = Path(a.call_store)
    if cs.is_dir() and any(cs.iterdir()):
        cstore = DayStore(cs)
        STRUCTURES.append("condor")
    print(f"put store: {a.store}")
    print(f"call store: {a.call_store if cstore else '(none)'}")
    print(f"structures runnable: {STRUCTURES}")
    earn = load_earnings()
    tk = set()
    for d in [x for x in store.dates if TRAIN[0] <= x <= VALID[1]][::40]:
        tk |= set(store.day(d)["ticker"].unique())
    earn = {t: v for t, v in earn.items() if t in tk}
    print(f"earnings calendars in the option universe: {len(earn)} tickers, "
          f"{sum(len(v) for v in earn.values()):,} dates")
    sig = build_signals(tk)
    print(f"{len(sig)} eligibility panels ({(time.time()-t0)/60:.1f}min)\n")

    w = (date(2018, 1, 1), date(2018, 12, 31))
    a = run("earnings", "put_spread", 0.30, 5.0, store, cstore, sig, earn, w)
    b = run("control", "put_spread", 0.30, 5.0, store, cstore, sig, earn, w)
    bites = a and b and (a["n"], round(a["mean"], 4)) != (b["n"], round(b["mean"], 4))
    print(f"KNOB PROOF arm: earnings n={a['n'] if a else 0} vs control "
          f"n={b['n'] if b else 0} -> {'BITES' if bites else 'INERT — ABORT'}")
    if not bites:
        return 1

    print(f"\n=== TRAIN {TRAIN[0]}..{TRAIN[1]} (search, not evidence) ===")
    results = []
    for st in STRUCTURES:
        for arm in ARMS:
            for dlt in DELTAS:
                for wd in WIDTHS:
                    r = run(arm, st, dlt, wd, store, cstore, sig, earn, TRAIN)
                    results.append({"arm": arm, "structure": st, "delta": dlt,
                                    "width": wd, "train": r})
                    show(f"{st} {arm} d{dlt} w{wd:.0f}", r)

    ctrl = {(x["structure"], x["delta"], x["width"]): x["train"]
            for x in results if x["arm"] == "control"}
    promoted = []
    for x in results:
        r = x["train"]
        if x["arm"] == "control" or not r:
            continue
        c = ctrl.get((x["structure"], x["delta"], x["width"]))
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
        v = run("earnings", x["structure"], x["delta"], x["width"], store,
                cstore, sig, earn, VALID)
        cv = run("control", x["structure"], x["delta"], x["width"], store,
                 cstore, sig, earn, VALID)
        x["validate"] = v
        show(f"{x['structure']} earnings d{x['delta']} w{x['width']:.0f}", v)
        if v and cv:
            lo, _ = boot_ci(v["pnls"])
            print(f"    control ${cv['mean']:.2f} -> "
                  f"{'HOLDS' if lo > 0 and v['mean'] > cv['mean'] else 'fails'}")

    out = DATA / f"earnings_crush_study_{a.tag}.json"
    out.write_text(json.dumps(
        {"generated": datetime.now().isoformat(), "structures": STRUCTURES,
         "results": [{k: ({kk: vv for kk, vv in val.items() if kk != "pnls"}
                          if isinstance(val, dict) else val)
                      for k, val in x.items()} for x in results]},
        indent=1, default=str), encoding="utf-8")
    print(f"\n({(time.time()-t0)/60:.1f}min) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
