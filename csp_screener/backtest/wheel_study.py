"""
AMENDMENT 15 — the wheel: cash-secured put -> assignment -> covered calls ->
called away. Measured per CYCLE against buy-and-hold of the same name over
the same days, and as a single-slot $1,200 account running one wheel at a
time.

A cycle: sell a 0.30-delta put (25-45 DTE). If it expires worthless the cycle
ends with the credit. If assigned, hold 100 shares at basis = strike - credit
and sell covered calls each month until called away; stuck positions
(> 365 days in stock, or the name delists) are liquidated at the last price
and COUNTED. Friction: $1/contract on every option leg opened or bought
back, 5%/10% slippage on premium; assignment and call-away at the strike.

    python csp_screener/backtest/wheel_study.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd

from csp_screener.backtest.build_call_store import CALL_STORE
from csp_screener.backtest.day_store import DayStore, STORE
from csp_screener.backtest.longput_study import (COMMISSION, DATA, SLIP, TRAIN,
                                                 VALID, boot_ci, build_signals,
                                                 flags)
from csp_screener.backtest.pattern_study import load_ohlc

PRICE_CAPS = [12.0, 50.0]
PUT_EXITS = ["expiry", "tp50"]
CALL_RULES = ["above_basis", "delta30"]

DELTA = 0.30
DTE_LO, DTE_HI = 25, 45
MIN_OI = 500
MAX_PER_DAY = 3
MAX_STOCK_DAYS = 365
MIN_CREDIT = 0.05
ACCOUNT = 1200.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_OHLC = {}
_SPLITS = {}


def stock_close(t, d):
    """As-traded close from the stock file (fallback when the store lacks
    the ticker on a day). None if the name has no bar on/after d within 10d."""
    if t not in _OHLC:
        p = DATA / "stocks" / f"{t}.csv"
        try:
            _OHLC[t] = load_ohlc(p)["c"] if p.exists() else None
        except Exception:
            _OHLC[t] = None
    s = _OHLC[t]
    if s is None:
        return None
    ts = pd.Timestamp(d)
    w = s[(s.index >= ts) & (s.index <= ts + pd.Timedelta(days=10))]
    return float(w.iloc[0]) if len(w) else None


def split_today(t, d):
    if t not in _SPLITS:
        rows = []
        p = DATA / "splits" / f"{t}.csv"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines()[1:]:
                try:
                    dd, r = line.split(",")
                    rows.append((date.fromisoformat(dd), float(r)))
                except ValueError:
                    pass
        _SPLITS[t] = dict(rows)
    return _SPLITS[t].get(d)


def spot_on(rows, t, d):
    u = rows[rows["ticker"] == t]["underlying_price"]
    return float(u.iloc[0]) if len(u) else stock_close(t, d)


def pick_put(rows, t, asof):
    c = rows[rows["ticker"] == t]
    if c.empty:
        return None
    dte = np.array([(e - asof).days for e in c["expiration"]])
    c = c[(dte >= DTE_LO) & (dte <= DTE_HI) & (c["bid"] > 0) & (c["ask"] > 0)
          & (c["open_interest"] >= MIN_OI) & c["delta"].notna()]
    c = c[c["strike"] < c["underlying_price"]]
    if c.empty:
        return None
    return c.assign(d=(c["delta"].abs() - DELTA).abs()).nsmallest(1, "d").iloc[0]


def pick_call(crows, t, asof, rule, basis):
    c = crows[crows["ticker"] == t]
    if c.empty:
        return None
    dte = np.array([(e - asof).days for e in c["expiration"]])
    c = c[(dte >= DTE_LO) & (dte <= DTE_HI) & (c["bid"] > 0) & (c["ask"] > 0)
          & (c["open_interest"] >= MIN_OI) & c["delta"].notna()
          & (c["strike"] > c["underlying_price"])]
    if c.empty:
        return None
    if rule == "above_basis":
        c = c[(c["strike"] >= basis) & (c["bid"] >= MIN_CREDIT)]
        if c.empty:
            return None                       # sit uncovered this cycle
        return c.nsmallest(1, "strike").iloc[0]
    return c.assign(d=(c["delta"] - DELTA).abs()).nsmallest(1, "d").iloc[0]


def opt_quote(rows, t, exp, k):
    m = rows[(rows["ticker"] == t) & (rows["expiration"] == exp)
             & (rows["strike"] == k)]
    if len(m) and float(m.iloc[0]["ask"]) > 0:
        return float(m.iloc[0]["bid"]), float(m.iloc[0]["ask"])
    return None


# ---------------------------------------------------------------------------
# the lifecycle
# ---------------------------------------------------------------------------

def run(cap, put_exit, call_rule, store, cstore, sig, window, band="pess",
        seed=11, single_slot=False):
    slip = SLIP[band]
    rng = np.random.default_rng(seed)
    days = [d for d in store.dates if window[0] <= d <= window[1]]
    active = {}                       # ticker -> position state
    cycles = []
    equity = ACCOUNT
    curve = []
    cday = {}

    def cday_rows(d):
        if d not in cday:
            cday.clear()
            cday[d] = cstore.day(d)
        return cday[d]

    def finish(t, p, pnl, d, reason):
        held = (d - p["start"]).days
        # matched control: buy-and-hold 100 shares from the same entry
        s0, s1 = p["spot_start"], spot_on(store.day(d), t, d)
        bh = (s1 - s0) * 100 if (s1 is not None and s0) else np.nan
        cycles.append({"ticker": t, "start": p["start"], "end": d, "days": held,
                       "pnl": pnl, "collateral": p["collateral"],
                       "entered_stock": p["phase_hits"] > 0, "reason": reason,
                       "stock_days": p["stock_days"], "buy_hold": bh,
                       "max_dd": p["max_dd"]})
        del active[t]

    for d in days:
        rows = store.day(d)

        # ---- manage every active wheel
        for t in list(active):
            p = active[t]
            if p["phase"] == "put":
                dte = (p["exp"] - d).days
                q = opt_quote(rows, t, p["exp"], p["k"])
                if put_exit == "tp50" and q and q[1] <= 0.5 * p["credit"]:
                    fric = COMMISSION + slip * q[1] * 100
                    p["pnl"] += (p["credit"] - q[1]) * 100 - fric
                    finish(t, p, p["pnl"], d, "tp50")
                    continue
                if dte > 0:
                    continue
                spot = spot_on(rows, t, d)
                if spot is None:
                    finish(t, p, p["pnl"] + p["credit"] * 100, d, "delisted_in_put")
                    continue
                if spot >= p["k"]:
                    p["pnl"] += p["credit"] * 100
                    finish(t, p, p["pnl"], d, "put_expired")
                    continue
                # assigned
                p["pnl"] += p["credit"] * 100
                p.update({"phase": "stock", "shares": 100,
                          "basis": p["k"] - p["credit"], "stock_start": d,
                          "call": None, "phase_hits": p["phase_hits"] + 1,
                          "peak": p["k"]})
                continue

            # ---- stock phase
            r = split_today(t, d)
            if r and r > 0:
                p["shares"] = int(round(p["shares"] * r))
                p["basis"] /= r
                if p["call"]:
                    p["call"]["k"] /= r
            spot = spot_on(rows, t, d)
            if spot is None:
                p["stock_days"] = (d - p["stock_start"]).days
                last = p.get("last_spot", p["basis"])
                finish(t, p, p["pnl"] + (last - p["basis"]) * p["shares"], d,
                       "delisted_in_stock")
                continue
            p["last_spot"] = spot
            unreal = (spot - p["basis"]) * p["shares"]
            p["max_dd"] = min(p["max_dd"], unreal)
            p["stock_days"] = (d - p["stock_start"]).days
            if p["stock_days"] > MAX_STOCK_DAYS:
                finish(t, p, p["pnl"] + unreal, d, "stuck_365d")
                continue
            c = p["call"]
            if c:
                if (c["exp"] - d).days > 0:
                    continue
                if spot >= c["k"]:                       # called away
                    p["pnl"] += (c["k"] - p["basis"]) * p["shares"]
                    finish(t, p, p["pnl"], d, "called_away")
                    continue
                p["call"] = None                          # expired, keep premium
            crows = cday_rows(d) if cstore is not None else rows.iloc[0:0]
            row = pick_call(crows, t, d, call_rule, p["basis"])
            if row is None:
                continue
            credit = float(row["bid"])
            p["pnl"] += credit * p["shares"] - (COMMISSION + slip * credit * 100)
            p["call"] = {"exp": row["expiration"], "k": float(row["strike"])}

        # ---- new wheels
        if single_slot and active:
            curve.append((d, equity + sum(0 for _ in active)))
            continue
        cands = []
        for t in rows["ticker"].unique():
            if t in active:
                continue
            f = flags(sig, t, d)
            if not f or not f[0]:
                continue
            u = rows[rows["ticker"] == t]["underlying_price"]
            if not len(u) or float(u.iloc[0]) > cap:
                continue
            cands.append(t)
        if not cands:
            continue
        limit = 1 if single_slot else MAX_PER_DAY
        if len(cands) > limit:
            cands = list(rng.choice(sorted(cands), limit, replace=False))
        for t in cands:
            row = pick_put(rows, t, d)
            if row is None:
                continue
            k, credit = float(row["strike"]), float(row["bid"])
            if credit < MIN_CREDIT:
                continue
            if single_slot and k * 100 > equity:
                continue
            fric = COMMISSION + slip * credit * 100
            active[t] = {"phase": "put", "exp": row["expiration"], "k": k,
                         "credit": credit, "start": d, "pnl": -fric,
                         "spot_start": float(row["underlying_price"]),
                         "collateral": k * 100, "phase_hits": 0,
                         "stock_days": 0, "max_dd": 0.0}
            if single_slot:
                break

    # liquidate anything still active at the window end (counted)
    for t in list(active):
        p = active[t]
        d = days[-1]
        if p["phase"] == "stock":
            spot = p.get("last_spot", p["basis"])
            finish(t, p, p["pnl"] + (spot - p["basis"]) * p["shares"], d, "window_end_stock")
        else:
            finish(t, p, p["pnl"], d, "window_end_put")

    if not cycles:
        return None
    df = pd.DataFrame(cycles)
    a = df["pnl"].to_numpy()
    bh = df["buy_hold"].dropna().to_numpy()
    roc = (df["pnl"] / df["collateral"]) / (df["days"].clip(lower=1) / 365.0)
    out = {
        "n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
        "total": float(a.sum()), "win": float((a > 0).mean()),
        "n_names": int(df["ticker"].nunique()),
        "entered_stock_pct": float(df["entered_stock"].mean()),
        "stuck_pct": float(df["reason"].isin(["stuck_365d", "delisted_in_stock"]).mean()),
        "mean_days": float(df["days"].mean()),
        "roc_annual_mean": float(roc.mean()),
        "worst_stock_dd": float(df["max_dd"].min()),
        "buy_hold_mean": float(bh.mean()) if len(bh) else float("nan"),
        "top_share": (float(a.max() / a.sum()) if a.sum() > 0 else float("nan")),
        "pnls": a.tolist(),
    }
    if single_slot:
        out["final_equity"] = float(ACCOUNT + a.sum())
    return out


def show(tag, r):
    if not r:
        print(f"  {tag:30} no cycles")
        return
    lo, hi = boot_ci(r["pnls"])
    print(f"  {tag:30} n={r['n']:4} win={r['win']:4.0%} per-cycle=${r['mean']:8.2f} "
          f"CI[{lo:8.2f},{hi:7.2f}] buy&hold=${r['buy_hold_mean']:8.2f} "
          f"stock={r['entered_stock_pct']:3.0%} stuck={r['stuck_pct']:3.0%} "
          f"ROC={r['roc_annual_mean']:+6.1%}/yr", flush=True)


def main() -> int:
    t0 = time.time()
    store = DayStore(STORE)
    cstore = DayStore(CALL_STORE)
    print(f"put store {len(store.dates)} days | call store {len(cstore.dates)} days")
    probe = pd.concat([cstore.day(d) for d in cstore.dates[::200]])
    if not (probe["open_interest"] > 0).any():
        print("REFUSING TO RUN: call store carries no open interest.")
        return 1
    tk = set()
    for d in [x for x in store.dates if TRAIN[0] <= x <= VALID[1]][::40]:
        tk |= set(store.day(d)["ticker"].unique())
    sig = build_signals(tk)
    print(f"{len(sig)} panels ({(time.time()-t0)/60:.1f}min)\n")

    w = (date(2018, 1, 1), date(2018, 12, 31))
    a = run(12.0, "expiry", "above_basis", store, cstore, sig, w)
    b = run(12.0, "expiry", "delta30", store, cstore, sig, w)
    bites = a and b and (a["n"], round(a["mean"], 2)) != (b["n"], round(b["mean"], 2))
    print(f"KNOB PROOF call rule: n={a['n'] if a else 0}/{b['n'] if b else 0} "
          f"-> {'BITES' if bites else 'INERT — ABORT'}")
    if not bites:
        return 1

    print(f"\n=== TRAIN {TRAIN[0]}..{TRAIN[1]} per-cycle (search, not evidence) ===")
    results = []
    for cap in PRICE_CAPS:
        for pe in PUT_EXITS:
            for cr in CALL_RULES:
                r = run(cap, pe, cr, store, cstore, sig, TRAIN)
                results.append({"arm": "wheel", "cap": cap, "put_exit": pe,
                                "call_rule": cr, "train": r})
                show(f"<=${cap:.0f} {pe} {cr}", r)

    print(f"\n=== single-slot ${ACCOUNT:.0f} account, one wheel at a time, TRAIN ===")
    slots = []
    for cap in PRICE_CAPS:
        for pe in PUT_EXITS:
            for cr in CALL_RULES:
                r = run(cap, pe, cr, store, cstore, sig, TRAIN, single_slot=True)
                slots.append({"cap": cap, "put_exit": pe, "call_rule": cr,
                              "train": ({k: v for k, v in r.items() if k != "pnls"}
                                        if r else None)})
                if r:
                    print(f"  <=${cap:.0f} {pe:6} {cr:12} cycles={r['n']:3} "
                          f"final=${r['final_equity']:8.0f} "
                          f"({100*(r['final_equity']/ACCOUNT-1):+.0f}%) "
                          f"stuck={r['stuck_pct']:.0%}", flush=True)

    promoted = []
    for x in results:
        r = x["train"]
        if not r:
            continue
        if (r["n"] >= 100 and r["mean"] > 0 and r["median"] > 0
                and (np.isnan(r["top_share"]) or r["top_share"] <= 0.40)
                and not np.isnan(r["buy_hold_mean"]) and r["mean"] > r["buy_hold_mean"]):
            promoted.append(x)
    print(f"\nTRAIN: {len(results)} configs, {len(promoted)} met the pre-registered bar")

    print(f"\n=== VALIDATE {VALID[0]}..{VALID[1]} ===")
    if not promoted:
        print("  none promoted — the validation window stays shut")
    for x in promoted:
        v = run(x["cap"], x["put_exit"], x["call_rule"], store, cstore, sig, VALID)
        x["validate"] = v
        show(f"<=${x['cap']:.0f} {x['put_exit']} {x['call_rule']}", v)
        if v:
            lo, _ = boot_ci(v["pnls"])
            ok = lo > 0 and v["mean"] > v["buy_hold_mean"]
            print(f"    -> {'HOLDS' if ok else 'fails'}")

    # every wheel arm carries its matched buy-and-hold as the control number;
    # the exporter reads it from the same record
    for x in results:
        if x["train"]:
            x["control_note"] = "buy_hold_mean is the matched control"

    out = DATA / "wheel_study.json"
    out.write_text(json.dumps(
        {"generated": datetime.now().isoformat(),
         "results": [{k: ({kk: vv for kk, vv in val.items() if kk != "pnls"}
                          if isinstance(val, dict) else val)
                      for k, val in x.items()} for x in results],
         "single_slot": slots},
        indent=1, default=str), encoding="utf-8")
    print(f"\n({(time.time()-t0)/60:.1f}min) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
