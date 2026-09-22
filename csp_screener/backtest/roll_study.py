"""
AMENDMENT 17 — does the roll work?

The owner's live NVDA record runs on a mechanic no prior amendment tested:
when a weekly short put goes against him he buys it back at a loss and, in
the same second, sells the next week's LOWER strike for a multiple of the
original credit (vol is up, the strike is nearer). Twice that funded the
loss and more.

This replays that decision over ~215 NVDA weekly cycles, 2019-2023, against
the honest control — take the loss and start fresh.

THE UNIT IS THE SEQUENCE. A roll chain is one economic decision: entry,
every roll, and the final close, summed. Scoring the legs separately is what
makes rolling look free.

    python csp_screener/backtest/roll_study.py
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

from csp_screener.backtest.day_store import DayStore
from csp_screener.backtest.longput_study import DATA, SLIP, boot_ci
from csp_screener.backtest.megacap_study import MEGA_STORE
from csp_screener.backtest.nvda_spec_review import load_splits
from csp_screener.backtest.tail_study import load_earnings

TICKER = "NVDA"
CONTRACTS = 4
COMMISSION = 1.00
TRAIN = (date(2019, 1, 1), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 29))

POLICIES = ["assign", "roll", "roll3"]          # "assign" = CONTROL
ROLL_STRIKES = ["min_strike", "max_credit"]   # see Amendment 17A
DTE_BANDS = [(3, 10), (5, 12)]
MIN_OI = 100
MIN_CREDIT = 0.05


def expected_move(puts, calls, spot, exp):
    p = puts[(puts["expiration"] == exp) & (puts["bid"] > 0) & (puts["ask"] > 0)]
    c = calls[(calls["expiration"] == exp) & (calls["bid"] > 0) & (calls["ask"] > 0)]
    if p.empty or c.empty:
        return None
    k = p.iloc[(p["strike"] - spot).abs().argsort().iloc[0]]["strike"]
    pr, cr = p[p["strike"] == k], c[c["strike"] == k]
    if pr.empty or cr.empty:
        return None
    return (0.5 * (float(pr.iloc[0]["bid"]) + float(pr.iloc[0]["ask"]))
            + 0.5 * (float(cr.iloc[0]["bid"]) + float(cr.iloc[0]["ask"])))


def pick(puts, exp, ref, one_lower=False):
    q = puts[(puts["expiration"] == exp) & (puts["bid"] > 0) & (puts["ask"] > 0)
             & (puts["open_interest"] >= MIN_OI)]
    below = q[q["strike"] <= ref].sort_values("strike", ascending=False)
    if below.empty:
        return None
    if one_lower:
        return below.iloc[1] if len(below) > 1 else None
    return below.iloc[0]


def quote(rows, exp, k):
    m = rows[(rows["expiration"] == exp) & (rows["strike"] == k)]
    if len(m) and float(m.iloc[0]["ask"]) > 0 and float(m.iloc[0]["bid"]) > 0:
        return float(m.iloc[0]["bid"]), float(m.iloc[0]["ask"])
    return None


def run(policy, roll_strike, dte_band, pstore, cstore, earn, splits,
        window, band="pess"):
    slip = SLIP[band]
    lo_d, hi_d = dte_band
    one_lower = roll_strike == "max_credit"   # True => highest strike
    days = [d for d in pstore.dates if window[0] <= d <= window[1]]
    seqs = []
    pos = None          # {'exp','k','credit'}  open short put
    seq = None          # {'pnl','legs','start','rolls','peak_loss'}

    for d in days:
        pv = pstore.day(d)
        pv = pv[pv["ticker"] == TICKER] if not pv.empty else pv
        if pv.empty:
            continue
        spot = float(pv.iloc[0]["underlying_price"])

        # ---------- manage an open position at its expiry ----------
        if pos is not None and (pos["exp"] - d).days <= 0:
            if any(seq["start"] < sd <= d for sd in splits):
                pos, seq = None, None                     # unadjustable
                continue
            intrinsic = max(pos["k"] - spot, 0.0)
            if intrinsic <= 0:                            # expired worthless
                seq["pnl"] += pos["credit"] * CONTRACTS * 100 - \
                    (CONTRACTS * COMMISSION + slip * pos["credit"] * CONTRACTS * 100)
                seqs.append({**seq, "end": d, "outcome": "expired_otm"})
                pos, seq = None, None
            else:
                # CHALLENGED. Control takes the loss; roll arms try to roll.
                q = quote(pv, pos["exp"], pos["k"])
                buyback = q[1] if q else intrinsic
                leg_pnl = (pos["credit"] - buyback) * CONTRACTS * 100 - \
                    (2 * CONTRACTS * COMMISSION
                     + slip * (pos["credit"] + buyback) * CONTRACTS * 100)
                can_roll = policy != "assign" and not (
                    policy == "roll3" and seq["rolls"] >= 3)
                new = None
                if can_roll:
                    # ROLL DOWN AND OUT FOR A NET CREDIT (Amendment 17A).
                    # Among next-week OTM strikes, take those whose bid still
                    # exceeds the cost of closing the loser; min_strike takes
                    # the lowest (most protection), max_credit the highest
                    # (most income). Targeting the expected-move strike — the
                    # original 17 spec — pays less than the buyback and made
                    # the roll impossible, which is why the knob was inert.
                    nxt = sorted({e for e in pv["expiration"]
                                  if lo_d <= (e - d).days <= hi_d})
                    for e in nxt:
                        if any(d < x <= e for x in earn.get(TICKER, [])):
                            continue
                        q = pv[(pv["expiration"] == e) & (pv["bid"] > buyback)
                               & (pv["ask"] > 0)
                               & (pv["open_interest"] >= MIN_OI)
                               & (pv["strike"] < spot)]
                        if q.empty:
                            continue
                        new = (q.nsmallest(1, "strike") if not one_lower
                               else q.nlargest(1, "strike")).iloc[0]
                        break
                seq["pnl"] += leg_pnl
                seq["peak_loss"] = min(seq["peak_loss"], seq["pnl"])
                if new is None:
                    seqs.append({**seq, "end": d,
                                 "outcome": "assigned" if policy == "assign"
                                 else "no_roll_available"})
                    pos, seq = None, None
                else:
                    seq["rolls"] += 1
                    pos = {"exp": new["expiration"], "k": float(new["strike"]),
                           "credit": float(new["bid"])}
                    continue

        if pos is not None:
            continue

        # ---------- open a fresh sequence ----------
        cv = cstore.day(d)
        cv = cv[cv["ticker"] == TICKER] if not cv.empty else cv
        if cv.empty:
            continue
        exps = sorted({e for e in pv["expiration"]
                       if lo_d <= (e - d).days <= hi_d})
        if not exps:
            continue
        exp = exps[0]
        if any(d < x <= exp for x in earn.get(TICKER, [])):
            continue
        em = expected_move(pv, cv, spot, exp)
        if em is None:
            continue
        row = pick(pv, exp, spot - em)
        if row is None or float(row["bid"]) < MIN_CREDIT:
            continue
        pos = {"exp": exp, "k": float(row["strike"]), "credit": float(row["bid"])}
        seq = {"pnl": 0.0, "start": d, "rolls": 0, "peak_loss": 0.0,
               "k0": pos["k"], "spot0": spot}

    if not seqs:
        return None
    df = pd.DataFrame(seqs)
    a = df["pnl"].to_numpy()
    return {
        "n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
        "total": float(a.sum()), "win": float((a > 0).mean()),
        "worst": float(a.min()),
        "mean_rolls": float(df["rolls"].mean()),
        "max_rolls": int(df["rolls"].max()),
        "rolled_pct": float((df["rolls"] > 0).mean()),
        "worst_peak_loss": float(df["peak_loss"].min()),
        "top_share": (float(a.max() / a.sum()) if a.sum() > 0 else float("nan")),
        "pnls": a.tolist(),
    }


def show(tag, r):
    if not r:
        print(f"  {tag:34} no sequences")
        return
    lo, hi = boot_ci(r["pnls"])
    print(f"  {tag:34} n={r['n']:4} win={r['win']:5.1%} "
          f"per-seq=${r['mean']:9.2f} CI[{lo:9.2f},{hi:8.2f}] "
          f"med=${r['median']:8.2f} worst=${r['worst']:10.2f} "
          f"rolled={r['rolled_pct']:4.0%} maxroll={r['max_rolls']}", flush=True)


def main() -> int:
    t0 = time.time()
    ps = DayStore(MEGA_STORE)
    from csp_screener.backtest.build_call_store import CALL_STORE
    scratch = Path(r"C:\Users\Admin\AppData\Local\Temp\claude"
                   r"\C--Users-Admin-Desktop-Work-BBM-Claude-code"
                   r"\05d246f6-aefd-4b57-be97-722f88bedc5a\scratchpad\mega_c")
    cs = DayStore(scratch if scratch.is_dir() else CALL_STORE)
    earn, splits = load_earnings(), load_splits(TICKER)
    print(f"NVDA weeklies {ps.dates[0]} -> {ps.dates[-1]} | calls {len(cs.dates)} days")
    print(f"earnings {len(earn.get(TICKER, []))} | splits dropped {splits}\n")

    a = run("assign", "min_strike", (3, 10), ps, cs, earn, splits, TRAIN)
    b = run("roll", "min_strike", (3, 10), ps, cs, earn, splits, TRAIN)
    bites = a and b and (a["n"], round(a["mean"], 2)) != (b["n"], round(b["mean"], 2))
    print(f"KNOB PROOF policy: assign n={a['n'] if a else 0} vs roll "
          f"n={b['n'] if b else 0} -> {'BITES' if bites else 'INERT — ABORT'}")
    if not bites:
        return 1

    print(f"\n=== TRAIN {TRAIN[0]}..{TRAIN[1]} (search, not evidence) ===")
    results = []
    for pol in POLICIES:
        for rs in ROLL_STRIKES:
            for db in DTE_BANDS:
                if pol == "assign" and rs == "max_credit":
                    continue                    # roll strike is inert here
                r = run(pol, rs, db, ps, cs, earn, splits, TRAIN)
                results.append({"policy": pol, "roll_strike": rs,
                                "dte": f"{db[0]}-{db[1]}", "train": r})
                show(f"{pol} {rs} dte{db[0]}-{db[1]}", r)

    ctrl = {x["dte"]: x["train"] for x in results if x["policy"] == "assign"}
    promoted = []
    for x in results:
        r = x["train"]
        if x["policy"] == "assign" or not r:
            continue
        c = ctrl.get(x["dte"])
        if (r["n"] >= 100 and r["mean"] > 0 and r["median"] > 0
                and (np.isnan(r["top_share"]) or r["top_share"] <= 0.40)
                and c and r["mean"] > c["mean"]):
            promoted.append(x)
    print(f"\nTRAIN: {len(results)} runs, {len(promoted)} met the bar")

    print(f"\n=== VALIDATE {VALID[0]}..{VALID[1]} (NVDA fell ~50% in 2022) ===")
    if not promoted:
        print("  none promoted — the validation window stays shut")
    for x in promoted:
        db = tuple(int(v) for v in x["dte"].split("-"))
        v = run(x["policy"], x["roll_strike"], db, ps, cs, earn, splits, VALID)
        cv = run("assign", "min_strike", db, ps, cs, earn, splits, VALID)
        x["validate"] = v
        show(f"{x['policy']} {x['roll_strike']} dte{x['dte']}", v)
        if v and cv:
            lo, _ = boot_ci(v["pnls"])
            print(f"    assign control ${cv['mean']:.2f} -> "
                  f"{'HOLDS' if lo > 0 and v['mean'] > cv['mean'] else 'fails'}")

    out = DATA / "roll_study.json"
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
