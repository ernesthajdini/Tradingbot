"""
AMENDMENT 16 — does the mega-cap liquidity tier move the toll below the prize?

Runs the SAME 12 configurations twice: once on the frozen 40 mega-caps, once
on the existing sub-$60 archive. The contrast between the two IS the test —
everything else (delta, tenor, exits, gates, friction) is identical, so any
difference is the underlying tier and nothing else.

Reported alongside P&L, because they are the hypothesis:
  * the MEASURED round-trip toll on the contracts actually traded
  * the COLLATERAL each structure commits (affordability != profitability)

    python csp_screener/backtest/megacap_study.py
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
from csp_screener.backtest.longput_study import (COMMISSION, DATA, SLIP,
                                                 boot_ci, build_signals, flags)

MEGA_STORE = DATA / "daystore_mega"

TRAIN = (date(2019, 1, 1), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))

STRUCTURES = ["put", "spread5", "spread10"]
DELTAS = [0.20, 0.30]
EXITS = ["dte21", "tp50"]

DTE_LO, DTE_HI = 25, 45
EXIT_DTE = 21
MIN_OI_SHORT = 500
MIN_OI_LONG = 100
MIN_CREDIT = 0.05
MAX_PER_DAY = 3
WIDTH = {"put": None, "spread5": 5.0, "spread10": 10.0}
LEGS = {"put": 1, "spread5": 2, "spread10": 2}


def pick(rows, t, asof, target, width):
    """Short put (+ long wing). Returns the legs, credit and the spread paid."""
    c = rows[rows["ticker"] == t]
    if c.empty:
        return None
    dte = np.array([(e - asof).days for e in c["expiration"]])
    c = c[(dte >= DTE_LO) & (dte <= DTE_HI)]
    if c.empty:
        return None
    q = c[(c["bid"] > 0) & (c["ask"] > 0)]
    s = q[(q["open_interest"] >= MIN_OI_SHORT) & q["delta"].notna()
          & (q["strike"] < q["underlying_price"])]          # never sell ITM
    if s.empty:
        return None
    s = s.assign(d=(s["delta"].abs() - target).abs()).nsmallest(1, "d").iloc[0]
    short_mid = 0.5 * (float(s["bid"]) + float(s["ask"]))
    short_spr = float(s["ask"]) - float(s["bid"])
    out = {"exp": s["expiration"], "ks": float(s["strike"]), "kl": None,
           "credit": float(s["bid"]), "spot0": float(s["underlying_price"]),
           "mid_sum": short_mid, "spr_sum": short_spr}
    if width is None:
        out["collateral"] = float(s["strike"]) * 100
        return out if out["credit"] >= MIN_CREDIT else None

    longs = q[(q["expiration"] == s["expiration"])
              & (q["strike"] <= s["strike"] - width)
              & (q["strike"] >= s["strike"] - 1.5 * width)
              & (q["open_interest"] >= MIN_OI_LONG)]
    if longs.empty:
        return None
    l = longs.nlargest(1, "strike").iloc[0]
    out["kl"] = float(l["strike"])
    out["credit"] = float(s["bid"]) - float(l["ask"])
    out["mid_sum"] += 0.5 * (float(l["bid"]) + float(l["ask"]))
    out["spr_sum"] += float(l["ask"]) - float(l["bid"])
    out["collateral"] = (float(s["strike"]) - float(l["strike"])) * 100
    return out if out["credit"] >= MIN_CREDIT else None


def quote(rows, t, exp, k):
    m = rows[(rows["ticker"] == t) & (rows["expiration"] == exp)
             & (rows["strike"] == k)]
    if len(m) and float(m.iloc[0]["ask"]) > 0 and float(m.iloc[0]["bid"]) > 0:
        return float(m.iloc[0]["bid"]), float(m.iloc[0]["ask"])
    return None


def close_cost(rows, p):
    """Buy the short back at ASK, sell the wing at BID; intrinsic if unquotable
    (the conservative direction for a seller)."""
    u = rows[rows["ticker"] == p["t"]]["underlying_price"]
    spot = float(u.iloc[0]) if len(u) else p["spot0"]
    qs = quote(rows, p["t"], p["exp"], p["ks"])
    cost = qs[1] if qs else max(p["ks"] - spot, 0.0)
    if p["kl"] is not None:
        ql = quote(rows, p["t"], p["exp"], p["kl"])
        cost -= ql[0] if ql else max(p["kl"] - spot, 0.0)
    return max(cost, 0.0)


def run(structure, target, exit_rule, store, sig, window, band="pess", seed=11):
    slip = SLIP[band]
    width = WIDTH[structure]
    n_legs = LEGS[structure]
    rng = np.random.default_rng(seed)
    days = [d for d in store.dates if window[0] <= d <= window[1]]
    open_pos, closed, last_entry = [], [], {}

    for d in days:
        rows = store.day(d)
        if rows.empty:
            continue

        still = []
        for p in open_pos:
            dte = (p["exp"] - d).days
            cost = None
            due = dte <= 0 or (exit_rule == "dte21" and dte <= EXIT_DTE)
            if exit_rule == "tp50" and not due:
                cost = close_cost(rows, p)
                if cost <= 0.5 * p["credit"]:
                    due = True
            if not due:
                still.append(p)
                continue
            if cost is None:
                cost = close_cost(rows, p)
            fric = 2 * n_legs * COMMISSION + slip * (p["credit"] + cost) * 100
            closed.append({"entry_date": p["d0"], "ticker": p["t"],
                           "pnl": (p["credit"] - cost) * 100 - fric,
                           "credit": p["credit"], "collateral": p["collateral"],
                           "mid_sum": p["mid_sum"], "spr_sum": p["spr_sum"],
                           "spot0": p["spot0"], "held": (d - p["d0"]).days})
        open_pos = still

        cands = []
        for t in rows["ticker"].unique():
            f = flags(sig, t, d)
            if not f or not f[0]:
                continue
            if last_entry.get(t) and (d - last_entry[t]).days < 30:
                continue
            cands.append(t)
        if not cands:
            continue
        if len(cands) > MAX_PER_DAY:
            cands = list(rng.choice(sorted(cands), MAX_PER_DAY, replace=False))
        for t in cands:
            sp = pick(rows, t, d, target, width)
            if sp is None:
                continue
            sp.update({"t": t, "d0": d})
            open_pos.append(sp)
            last_entry[t] = d

    if not closed:
        return None
    df = pd.DataFrame(closed)
    a = df["pnl"].to_numpy()
    # the hypothesis, measured on the contracts actually traded
    toll = 100 * ((df["spr_sum"] * 100 + 2 * n_legs * COMMISSION)
                  / (df["mid_sum"] * 100)).median()
    return {
        "n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
        "total": float(a.sum()), "win": float((a > 0).mean()),
        "n_names": int(df["ticker"].nunique()),
        "toll_pct": float(toll),
        "median_credit": float(df["credit"].median() * 100),
        "median_collateral": float(df["collateral"].median()),
        "median_spot": float(df["spot0"].median()),
        "roc_pct": float((df["pnl"] / df["collateral"]).median() * 100),
        "top_share": (float(a.max() / a.sum()) if a.sum() > 0 else float("nan")),
        "pnls": a.tolist(),
    }


def show(tag, r):
    if not r:
        print(f"  {tag:34} no trades")
        return
    lo, hi = boot_ci(r["pnls"])
    print(f"  {tag:34} n={r['n']:4} win={r['win']:4.0%} toll={r['toll_pct']:5.1f}% "
          f"cr=${r['median_credit']:5.0f} coll=${r['median_collateral']:6.0f} "
          f"per-trade=${r['mean']:8.2f} CI[{lo:8.2f},{hi:7.2f}]", flush=True)


def main() -> int:
    t0 = time.time()
    if not MEGA_STORE.is_dir() or not any(MEGA_STORE.iterdir()):
        print(f"REFUSING TO RUN: {MEGA_STORE} does not exist. Build it first:\n"
              f"  python csp_screener/backtest/day_store.py "
              f"--options-subdir options_mega --store {MEGA_STORE}")
        return 1
    mega, base = DayStore(MEGA_STORE), DayStore(STORE)
    print(f"mega store {len(mega.dates)} days | base store {len(base.dates)} days")

    probe = pd.concat([mega.day(d) for d in mega.dates[::120]])
    if not (probe["open_interest"] > 0).any():
        print("REFUSING TO RUN: mega store carries no open interest.")
        return 1

    tiers = {}
    for lab, st in (("MEGA", mega), ("BASE", base)):
        tk = set()
        for d in [x for x in st.dates if TRAIN[0] <= x <= VALID[1]][::40]:
            tk |= set(st.day(d)["ticker"].unique())
        tiers[lab] = (st, build_signals(tk))
        print(f"  {lab}: {len(tiers[lab][1])} panels")
    print(f"({(time.time()-t0)/60:.1f}min)\n")

    a = run("put", 0.30, "dte21", *tiers["MEGA"], TRAIN)
    b = run("put", 0.30, "dte21", *tiers["BASE"], TRAIN)
    bites = a and b and round(a["toll_pct"], 2) != round(b["toll_pct"], 2)
    print(f"KNOB PROOF tier: MEGA toll={a['toll_pct'] if a else 0:.1f}% vs "
          f"BASE toll={b['toll_pct'] if b else 0:.1f}% -> "
          f"{'BITES' if bites else 'INERT — ABORT'}")
    if not bites:
        return 1

    results = []
    for lab in ("MEGA", "BASE"):
        st, sig = tiers[lab]
        print(f"\n=== {lab} tier, TRAIN {TRAIN[0]}..{TRAIN[1]} "
              f"(search, not evidence) ===")
        for stru in STRUCTURES:
            for dlt in DELTAS:
                for ex in EXITS:
                    r = run(stru, dlt, ex, st, sig, TRAIN)
                    results.append({"tier": lab, "structure": stru,
                                    "delta": dlt, "exit": ex, "train": r})
                    show(f"{stru} d{dlt} {ex}", r)

    ctrl = {(x["structure"], x["delta"], x["exit"]): x["train"]
            for x in results if x["tier"] == "BASE"}
    promoted = []
    for x in results:
        r = x["train"]
        if x["tier"] == "BASE" or not r:
            continue
        c = ctrl.get((x["structure"], x["delta"], x["exit"]))
        if (r["n"] >= 100 and r["mean"] > 0 and r["median"] > 0
                and (np.isnan(r["top_share"]) or r["top_share"] <= 0.40)
                and c and r["mean"] > c["mean"]):
            promoted.append(x)
    print(f"\nTRAIN: {len(results)} runs ({len(STRUCTURES)*len(DELTAS)*len(EXITS)} "
          f"declared configs x 2 tiers), {len(promoted)} met the bar")

    print(f"\n=== VALIDATE {VALID[0]}..{VALID[1]} ===")
    if not promoted:
        print("  none promoted — the validation window stays shut")
    for x in promoted:
        st, sig = tiers["MEGA"]
        v = run(x["structure"], x["delta"], x["exit"], st, sig, VALID)
        cs, csig = tiers["BASE"]
        cv = run(x["structure"], x["delta"], x["exit"], cs, csig, VALID)
        x["validate"] = v
        show(f"{x['structure']} d{x['delta']} {x['exit']}", v)
        if v and cv:
            lo, _ = boot_ci(v["pnls"])
            print(f"    base-tier control ${cv['mean']:.2f} -> "
                  f"{'HOLDS' if lo > 0 and v['mean'] > cv['mean'] else 'fails'}")

    print("\n=== AFFORDABILITY at $1,200 (separate question from profit) ===")
    for x in results:
        r = x["train"]
        if x["tier"] != "MEGA" or not r:
            continue
        pct = 100 * r["median_collateral"] / 1200
        print(f"  {x['structure']:8} d{x['delta']} median collateral "
              f"${r['median_collateral']:7.0f} = {pct:6.0f}% of the account"
              f"{'  <-- reachable' if pct <= 50 else ''}")

    out = DATA / "megacap_study.json"
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
