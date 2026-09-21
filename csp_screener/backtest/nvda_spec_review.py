"""
Review harness for the owner's NVDA cash-secured-put spec.

Runs the SPEC'S OWN strike-selection algorithm against real NVDA option
chains, 2019-2023, so the design can be judged on what it actually selects
rather than on how it reads:

    Expected Move     = ATM call mid + ATM put mid       (the straddle)
    Downside Ref      = spot - Expected Move
    strike            = highest strike <= Downside Ref   ("at_or_below")
                        or one strike lower              ("one_lower")

Everything else follows the spec: 3-10 calendar DTE, reject any expiration
containing an earnings date, two-sided quotes only, one contract.

Fills are reported as a BAND because the spec says "limit near the midpoint",
which is achievable sometimes and not always:
    MID  — sell at mid, buy back at mid   (optimistic, the spec's intent)
    CROSS— sell at bid, buy back at ask   (what you get when you must trade)
Commission $1.00/contract/side. Expiry settles at intrinsic.

    python csp_screener/backtest/nvda_spec_review.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd

from csp_screener.backtest.day_store import DayStore
from csp_screener.backtest.tail_study import load_earnings


def load_splits(ticker):
    """Split dates. A position held ACROSS one has a pre-split strike marked
    against a post-split spot — NVDA's 4:1 on 2021-07-20 manufactured a fake
    -$48,839 'loss' that WAS the whole of 2021 before this guard existed."""
    out = []
    f = DATA / "splits" / f"{ticker}.csv"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines()[1:]:
            try:
                out.append(date.fromisoformat(line.split(",")[0]))
            except (ValueError, IndexError):
                pass
    return sorted(out)

SCRATCH = Path(r"C:\Users\Admin\AppData\Local\Temp\claude"
               r"\C--Users-Admin-Desktop-Work-BBM-Claude-code"
               r"\05d246f6-aefd-4b57-be97-722f88bedc5a\scratchpad")
PUT_STORE, CALL_STORE = SCRATCH / "mega_p", SCRATCH / "mega_c"
DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"

TICKER = "NVDA"
DTE_LO, DTE_HI = 3, 10
COMMISSION = 1.00
MIN_OI = 100
RULES = ["at_or_below", "one_lower"]
EXITS = ["expiry", "tp80"]
FILLS = ["mid", "cross"]


def atm_straddle(puts, calls, spot, exp):
    """Expected Move per the spec: ATM call mid + ATM put mid."""
    p = puts[(puts["expiration"] == exp) & (puts["bid"] > 0) & (puts["ask"] > 0)]
    c = calls[(calls["expiration"] == exp) & (calls["bid"] > 0) & (calls["ask"] > 0)]
    if p.empty or c.empty:
        return None, None
    k = p.iloc[(p["strike"] - spot).abs().argsort().iloc[0]]["strike"]
    pr = p[p["strike"] == k]
    cr = c[c["strike"] == k]
    if pr.empty or cr.empty:
        return None, None
    pm = 0.5 * (float(pr.iloc[0]["bid"]) + float(pr.iloc[0]["ask"]))
    cm = 0.5 * (float(cr.iloc[0]["bid"]) + float(cr.iloc[0]["ask"]))
    return pm + cm, float(k)


def select(puts, exp, ref, rule):
    q = puts[(puts["expiration"] == exp) & (puts["bid"] > 0) & (puts["ask"] > 0)
             & (puts["open_interest"] >= MIN_OI)]
    if q.empty:
        return None
    below = q[q["strike"] <= ref].sort_values("strike", ascending=False)
    if below.empty:
        return None
    if rule == "one_lower":
        if len(below) < 2:
            return None
        return below.iloc[1]
    return below.iloc[0]


def run(rule, exit_rule, fill, pstore, cstore, earn, splits):
    days = pstore.dates
    rows = []
    skipped_split = 0
    open_pos = None
    for d in days:
        pv = pstore.day(d)
        if pv.empty:
            continue
        pv = pv[pv["ticker"] == TICKER]
        if pv.empty:
            continue
        spot = float(pv.iloc[0]["underlying_price"])

        # ---- manage
        if open_pos is not None:
            p = open_pos
            dte = (p["exp"] - d).days
            m = pv[(pv["expiration"] == p["exp"]) & (pv["strike"] == p["k"])]
            px = None
            if len(m) and float(m.iloc[0]["ask"]) > 0:
                px = (0.5 * (float(m.iloc[0]["bid"]) + float(m.iloc[0]["ask"]))
                      if fill == "mid" else float(m.iloc[0]["ask"]))
            due = dte <= 0
            if exit_rule == "tp80" and px is not None and px <= 0.2 * p["credit"]:
                due = True
            if due:
                if any(p["d0"] < sd <= d for sd in splits):
                    skipped_split += 1          # unadjustable across a split
                    open_pos = None
                    continue
                if px is None or dte <= 0:
                    px = max(p["k"] - spot, 0.0)          # settle intrinsic
                pnl = (p["credit"] - px) * 100 - 2 * COMMISSION
                rows.append({"entry": p["d0"], "exit": d, "strike": p["k"],
                             "credit": p["credit"], "exit_px": px, "pnl": pnl,
                             "spot0": p["spot0"], "spot1": spot,
                             "collateral": p["k"] * 100, "delta": p["delta"],
                             "pct_otm": p["pct_otm"], "em_pct": p["em_pct"],
                             "assigned": spot < p["k"], "dte0": p["dte0"]})
                open_pos = None
            else:
                continue

        if open_pos is not None:
            continue

        # ---- entry: nearest expiry 3-10 days out, no earnings before it
        cv = cstore.day(d)
        cv = cv[cv["ticker"] == TICKER] if not cv.empty else cv
        if cv.empty:
            continue
        exps = sorted({e for e in pv["expiration"]
                       if DTE_LO <= (e - d).days <= DTE_HI})
        if not exps:
            continue
        exp = exps[0]
        evs = earn.get(TICKER, [])
        if any(d < e <= exp for e in evs):          # SPEC: reject earnings weeks
            continue
        em, _ = atm_straddle(pv, cv, spot, exp)
        if em is None or em <= 0:
            continue
        row = select(pv, exp, spot - em, rule)
        if row is None:
            continue
        credit = (0.5 * (float(row["bid"]) + float(row["ask"])) if fill == "mid"
                  else float(row["bid"]))
        if credit <= 0:
            continue
        open_pos = {"exp": exp, "k": float(row["strike"]), "credit": credit,
                    "d0": d, "spot0": spot, "dte0": (exp - d).days,
                    "delta": (abs(float(row["delta"]))
                              if pd.notna(row["delta"]) else np.nan),
                    "pct_otm": 100 * (spot - float(row["strike"])) / spot,
                    "em_pct": 100 * em / spot}

    if not rows:
        return None
    df = pd.DataFrame(rows)
    a = df["pnl"].to_numpy()
    roc = df["pnl"] / df["collateral"]
    return {
        "n": int(len(a)), "win": float((a > 0).mean()),
        "mean": float(a.mean()), "median": float(np.median(a)),
        "total": float(a.sum()), "worst": float(a.min()),
        "assigned_pct": float(df["assigned"].mean()),
        "median_delta": float(df["delta"].median(skipna=True)),
        "median_pct_otm": float(df["pct_otm"].median()),
        "median_em_pct": float(df["em_pct"].median()),
        "median_credit": float(df["credit"].median() * 100),
        "median_collateral": float(df["collateral"].median()),
        "roc_total_pct": float(100 * roc.sum()),
        "worst_roc_pct": float(100 * roc.min()),
        "skipped_split": int(skipped_split),
        "df": df,
    }


def show(tag, r):
    if not r:
        print(f"  {tag:28} no trades")
        return
    print(f"  {tag:28} n={r['n']:4} win={r['win']:5.1%} "
          f"delta={r['median_delta']:.3f} OTM={r['median_pct_otm']:4.1f}% "
          f"cr=${r['median_credit']:5.0f} mean=${r['mean']:8.2f} "
          f"worst=${r['worst']:9.2f} assigned={r['assigned_pct']:4.1%}", flush=True)


def main() -> int:
    ps, cs = DayStore(PUT_STORE), DayStore(CALL_STORE)
    earn = load_earnings()
    splits = load_splits(TICKER)
    print(f"splits dropped from the sample: {splits}")
    print(f"NVDA chains: {len(ps.dates)} days {ps.dates[0]} -> {ps.dates[-1]}")
    print(f"NVDA earnings dates on file: {len(earn.get(TICKER, []))}\n")

    out = []
    for fill in FILLS:
        print(f"=== fills at {fill.upper()}"
              f"{'  (spec intent: limit near mid)' if fill == 'mid' else '  (crossing the spread)'} ===")
        for rule in RULES:
            for ex in EXITS:
                r = run(rule, ex, fill, ps, cs, earn, splits)
                show(f"{rule} {ex}", r)
                if r:
                    out.append({"fill": fill, "rule": rule, "exit": ex,
                                **{k: v for k, v in r.items() if k != "df"}})
        print()

    base = run("at_or_below", "expiry", "cross", ps, cs, earn, splits)
    if base:
        df = base["df"]
        print("=== what the rule actually SELECTS (at_or_below, hold to expiry) ===")
        print(f"  expected move the straddle implies : "
              f"{df['em_pct'].median():.2f}% of spot")
        print(f"  strike sits                        : "
              f"{df['pct_otm'].median():.2f}% below spot")
        print(f"  delta of the selected put          : "
              f"{df['delta'].median(skipna=True):.3f}")
        print(f"  credit as % of collateral          : "
              f"{100*(df['credit']*100/df['collateral']).median():.3f}% per trade")
        print(f"  median DTE at entry                : {df['dte0'].median():.0f} days")
        print()
        print("=== the tail: 5 worst trades ===")
        for _, x in df.nsmallest(5, "pnl").iterrows():
            print(f"  {x['entry']} -> {x['exit']}  K={x['strike']:7.2f} "
                  f"spot {x['spot0']:7.2f}->{x['spot1']:7.2f} "
                  f"({100*(x['spot1']/x['spot0']-1):+6.1f}%)  "
                  f"P&L ${x['pnl']:10.2f}  = {100*x['pnl']/x['collateral']:+6.2f}% of collateral")
        print()
        print("=== by calendar year ===")
        df["yr"] = [e.year for e in df["entry"]]
        for y, g in df.groupby("yr"):
            print(f"  {y}  n={len(g):3} win={100*(g.pnl>0).mean():5.1f}% "
                  f"total=${g.pnl.sum():10.2f}  worst=${g.pnl.min():9.2f}")

    p = DATA / "nvda_spec_review.json"
    p.write_text(json.dumps({"generated": datetime.now().isoformat(),
                             "results": out}, indent=1, default=str),
                 encoding="utf-8")
    print(f"\n-> {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
