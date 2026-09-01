"""
AMENDMENT 13 — is the left tail cuttable?

The owner's Learning page shows 13 winning tickers (+$223.52) erased by three
losers (-$220.27), of which BB alone is -$189.51. Net +$3.25 over 24 trades.
That is the short-put payoff in miniature: win often, win small, lose rarely,
lose everything.

So this asks the narrower question no amendment has asked: is the loss
concentrated in a tail, and can a rule available BEFORE entry remove it?

The earnings blackout is testable here for the FIRST time. Every earlier run
was stamped `earnings_gate: unavailable`; the ThetaData pull captured 107,940
earnings dates across 3,167 tickers. Earnings are published in advance, so
skipping them is a legitimate pre-entry filter, not post-hoc surgery.

    python csp_screener/backtest/tail_study.py
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

from csp_screener.backtest.day_store import DayStore, STORE
from csp_screener.backtest.longput_study import (COMMISSION, DATA, SLIP, TRAIN,
                                                 VALID, boot_ci, build_signals,
                                                 flags)

BLACKOUTS = ["none", "before_expiry", "before_exit"]     # "none" = CONTROL
DELTAS = [0.25, 0.30]
EXITS = ["dte21", "expiry"]

DTE_LO, DTE_HI = 25, 45
EXIT_DTE = 21
MIN_OI = 500                  # production's floor for SOLD contracts
MAX_PER_DAY = 3
MAX_COLLATERAL = 3000.0       # a $1,200 account cannot secure more than this


def load_earnings():
    """{ticker: sorted [date, ...]} from the pulled calendars."""
    out = {}
    for p in (DATA / "earnings").glob("*.csv"):
        ds = []
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip().strip('"')
            if len(s) >= 10 and s[:4].isdigit():
                try:
                    ds.append(date.fromisoformat(s[:10]))
                except ValueError:
                    pass
        if ds:
            out[p.stem] = sorted(ds)
    return out


def has_earnings(earn, t, lo, hi):
    """Any earnings date in (lo, hi]? Known at entry — published in advance."""
    ds = earn.get(t)
    if not ds:
        return False
    i = np.searchsorted(ds, lo, side="right")
    return i < len(ds) and ds[i] <= hi


def pick_put(rows, t, asof, target):
    c = rows[rows["ticker"] == t]
    if c.empty:
        return None
    dte = np.array([(e - asof).days for e in c["expiration"]])
    c = c[(dte >= DTE_LO) & (dte <= DTE_HI)]
    if c.empty:
        return None
    c = c[(c["bid"] > 0) & (c["ask"] > 0) & (c["open_interest"] >= MIN_OI)
          & c["delta"].notna()]
    # never sell ITM (the BB incident's guard, already in production)
    c = c[c["strike"] < c["underlying_price"]]
    if c.empty:
        return None
    return c.assign(d=(c["delta"].abs() - target).abs()).nsmallest(1, "d").iloc[0]


def run(blackout, target, exit_rule, store, sig, earn, window, band="pess",
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
            due = dte <= 0 or (exit_rule == "dte21" and dte <= EXIT_DTE)
            if not due:
                still.append(p)
                continue
            m = rows[(rows["ticker"] == p["t"]) &
                     (rows["expiration"] == p["exp"]) &
                     (rows["strike"] == p["k"])]
            # buying the put back: pay the ASK
            cost = float(m.iloc[0]["ask"]) if len(m) and float(
                m.iloc[0]["ask"]) > 0 else None
            if cost is None:
                u = rows[rows["ticker"] == p["t"]]["underlying_price"]
                spot = float(u.iloc[0]) if len(u) else p["spot0"]
                cost = max(p["k"] - spot, 0.0)   # seller PAYS intrinsic
            fric = 2 * COMMISSION + slip * (p["credit"] + cost) * 100
            closed.append({"entry_date": p["d0"], "ticker": p["t"],
                           "pnl": (p["credit"] - cost) * 100 - fric,
                           "credit": p["credit"], "earn": p["earn"],
                           "held": (d - p["d0"]).days})
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
            cands = list(rng.choice(cands, MAX_PER_DAY, replace=False))

        for t in cands:
            row = pick_put(rows, t, d, target)
            if row is None:
                continue
            exp = row["expiration"]
            k = float(row["strike"])
            if k * 100 > MAX_COLLATERAL:
                continue
            # blackout horizon: what the rule is allowed to see at entry
            # A dte21 position is closed when dte <= 21, i.e. on exp-21d.
            # The blackout may only consider earnings before that, not after.
            horizon = (exp if exit_rule == "expiry"
                       else max(d, exp - timedelta(days=EXIT_DTE)))
            e_before_expiry = has_earnings(earn, t, d, exp)
            e_before_exit = has_earnings(earn, t, d, horizon)
            if blackout == "before_expiry" and e_before_expiry:
                continue
            if blackout == "before_exit" and e_before_exit:
                continue
            open_pos.append({"t": t, "exp": exp, "k": k,
                             "credit": float(row["bid"]), "d0": d,
                             "earn": bool(e_before_expiry),
                             "spot0": float(row["underlying_price"])})
            last_entry[t] = d

    if not closed:
        return None
    df = pd.DataFrame(closed)
    a = df["pnl"].to_numpy()
    return {
        "n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
        "total": float(a.sum()), "win": float((a > 0).mean()),
        "n_dates": int(df["entry_date"].nunique()),
        "earn_share": float(df["earn"].mean()),
        "pnls": a.tolist(), "df": df,
    }


def decompose(r):
    """Where does the gross LOSS live, and how much of it is earnings?"""
    df = r["df"]
    a = df["pnl"].to_numpy()
    losses = a[a < 0]
    gross_loss = float(losses.sum())
    out = {"gross_loss": gross_loss, "gross_win": float(a[a > 0].sum())}
    order = np.argsort(a)                    # worst first
    for pct in (1, 5, 10):
        k = max(1, int(len(a) * pct / 100))
        worst = a[order[:k]]
        out[f"worst_{pct}pct"] = {
            "n": int(k), "sum": float(worst.sum()),
            "share_of_gross_loss": (float(worst.sum() / gross_loss)
                                    if gross_loss else float("nan")),
            "earn_share": float(df.iloc[order[:k]]["earn"].mean()),
        }
    return out


def show(tag, r):
    if not r:
        print(f"  {tag:36} no trades")
        return
    lo, hi = boot_ci(r["pnls"])
    print(f"  {tag:36} n={r['n']:4} win={r['win']:4.0%} "
          f"per-trade=${r['mean']:8.2f} CI[{lo:8.2f},{hi:7.2f}] "
          f"med=${r['median']:6.2f} earn%={r['earn_share']:4.0%}", flush=True)


def main() -> int:
    t0 = time.time()
    store = DayStore(STORE)
    earn = load_earnings()
    print(f"earnings calendars: {len(earn)} tickers, "
          f"{sum(len(v) for v in earn.values()):,} dates")

    tk = set()
    for d in [x for x in store.dates if TRAIN[0] <= x <= VALID[1]][::40]:
        tk |= set(store.day(d)["ticker"].unique())
    sig = build_signals(tk)
    print(f"{len(sig)} eligibility panels ({(time.time()-t0)/60:.1f}min)\n")

    # ---- knob-bite proof before any result is read
    w = (date(2018, 1, 1), date(2018, 12, 31))
    a = run("none", 0.30, "dte21", store, sig, earn, w)
    b = run("before_expiry", 0.30, "dte21", store, sig, earn, w)
    bites = a and b and a["n"] != b["n"]
    print(f"KNOB PROOF blackout: control n={a['n'] if a else 0} vs "
          f"blackout n={b['n'] if b else 0} -> "
          f"{'BITES' if bites else 'INERT — ABORT'}")
    if not bites:
        return 1

    print(f"\n=== TRAIN {TRAIN[0]}..{TRAIN[1]} (search, not evidence) ===")
    results, ctrl_runs = [], {}
    for bo in BLACKOUTS:
        for dlt in DELTAS:
            for ex in EXITS:
                r = run(bo, dlt, ex, store, sig, earn, TRAIN, "pess")
                results.append({"blackout": bo, "delta": dlt, "exit": ex,
                                "train": r})
                if bo == "none" and r:
                    ctrl_runs[(dlt, ex)] = r
                show(f"{bo} d{dlt} {ex}", r)

    print("\n=== TAIL DECOMPOSITION of the control arm (measurement) ===")
    base = ctrl_runs.get((0.30, "dte21"))
    dec = None
    if base:
        dec = decompose(base)
        print(f"  gross wins ${dec['gross_win']:>9,.0f} | "
              f"gross losses ${dec['gross_loss']:>9,.0f}")
        for pct in (1, 5, 10):
            d_ = dec[f"worst_{pct}pct"]
            print(f"  worst {pct:2}% ({d_['n']:3} trades) = "
                  f"${d_['sum']:>9,.0f} = {d_['share_of_gross_loss']:5.0%} of all "
                  f"losses | {d_['earn_share']:3.0%} had earnings in the hold")

    promoted = []
    for x in results:
        r = x["train"]
        if x["blackout"] == "none" or not r:
            continue
        c = ctrl_runs.get((x["delta"], x["exit"]))
        top = max(r["pnls"]) if r["pnls"] else 0
        if (r["n"] >= 100 and r["mean"] > 0 and r["median"] > 0
                and (r["total"] <= 0 or top / r["total"] <= 0.40)
                and c and r["mean"] > c["mean"]):
            promoted.append(x)
    print(f"\nTRAIN: {len(results)} configs, {len(promoted)} met the bar")

    print(f"\n=== VALIDATE {VALID[0]}..{VALID[1]} ===")
    if not promoted:
        print("  none promoted — the validation window stays shut")
    for x in promoted:
        v = run(x["blackout"], x["delta"], x["exit"], store, sig, earn, VALID,
                "pess")
        cv = run("none", x["delta"], x["exit"], store, sig, earn, VALID, "pess")
        x["validate"] = v
        show(f"{x['blackout']} d{x['delta']} {x['exit']}", v)
        if v and cv:
            lo, _ = boot_ci(v["pnls"])
            print(f"    control ${cv['mean']:.2f} -> "
                  f"{'HOLDS' if lo > 0 and v['mean'] > cv['mean'] else 'fails'}")

    out = DATA / "tail_study.json"
    out.write_text(json.dumps({
        "generated": datetime.now().isoformat(),
        "tail_decomposition": dec,
        "results": [{k: ({kk: vv for kk, vv in val.items()
                          if kk not in ("pnls", "df")}
                         if isinstance(val, dict) else val)
                     for k, val in x.items()} for x in results],
    }, indent=1, default=str), encoding="utf-8")
    print(f"\n({(time.time()-t0)/60:.1f}min) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
