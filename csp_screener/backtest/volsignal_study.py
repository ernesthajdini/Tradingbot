"""
AMENDMENT 10 — unusual put volume.

Every prior study read the stock's chart. This one reads the OPTIONS MARKET:
`volume` and `open_interest`, two columns no earlier amendment touched. A put
trading three times its own open interest is somebody taking a position, and
that information exists nowhere in the price data.

The instrument is identical across all three arms — same delta target, same
DTE window, same liquidity gates. Only the entry TRIGGER differs, so any
difference between arms is the signal and not the contract.

Entry is the NEXT trading day. Volume is a full-session quantity; filling on
the same session's close would use information that is not complete until
after that close.

    python csp_screener/backtest/volsignal_study.py
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

from csp_screener.backtest.day_store import DayStore, STORE
from csp_screener.backtest.longput_study import (
    COMMISSION, DATA, DTE_HI, DTE_LO, EXIT_DTE, EXITS, MAX_PREMIUM, MIN_OI,
    SLIP, TRAIN, VALID, boot_ci, build_signals, flags, pick_put, show)

TRIGGERS = ["contract_vol_3x", "ticker_vol_3x", "none"]     # "none" = CONTROL
DELTAS = [0.30, 0.40]
VOL_MULT = 3.0
VOL_LOOKBACK = 20
MIN_BASE_VOL = 50           # a name must trade some puts before "3x" means anything
MAX_PER_DAY = 3


def triggered(rows, hist, trigger):
    """Tickers flagged on THIS session. Entry happens the next session."""
    if trigger == "none":
        return set(rows["ticker"].unique())
    if trigger == "contract_vol_3x":
        q = rows[(rows["open_interest"] >= MIN_OI) & (rows["volume"] > 0)]
        return set(q[q["volume"] >= VOL_MULT * q["open_interest"]]["ticker"])
    # ticker_vol_3x
    today = rows.groupby("ticker")["volume"].sum()
    out = set()
    for t, v in today.items():
        h = hist.get(t)
        if h and len(h) >= VOL_LOOKBACK:
            base = float(np.mean(h))
            if base >= MIN_BASE_VOL and v >= VOL_MULT * base:
                out.add(t)
    return out


def run(trigger, target, exit_rule, store, sig, window, band="pess", seed=11):
    slip = SLIP[band]
    rng = np.random.default_rng(seed)
    days = [d for d in store.dates if window[0] <= d <= window[1]]
    open_pos, closed, last_entry = [], [], {}
    hist = defaultdict(lambda: deque(maxlen=VOL_LOOKBACK))
    pending = set()

    for d in days:
        rows = store.day(d)

        # ---- manage open positions on this session's real quotes
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

        # ---- act on YESTERDAY's triggers, filtered to today's eligibility
        cands = []
        for t in pending:
            f = flags(sig, t, d)
            if not f or not f[0]:
                continue
            if last_entry.get(t) and (d - last_entry[t]).days < 30:
                continue
            cands.append(t)
        if len(cands) > MAX_PER_DAY:
            cands = list(rng.choice(sorted(cands), MAX_PER_DAY, replace=False))
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

        # ---- compute today's triggers for tomorrow, then update history
        pending = triggered(rows, hist, trigger)
        for t, v in rows.groupby("ticker")["volume"].sum().items():
            hist[t].append(float(v))

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


def main() -> int:
    t0 = time.time()
    store = DayStore(STORE)
    print(f"store: {len(store.dates)} days {store.dates[0]} -> {store.dates[-1]}")
    tk = set()
    for d in [x for x in store.dates if TRAIN[0] <= x <= VALID[1]][::40]:
        tk |= set(store.day(d)["ticker"].unique())
    print(f"{len(tk)} tickers; building eligibility panels...", flush=True)
    sig = build_signals(tk)
    print(f"{len(sig)} panels ({(time.time()-t0)/60:.1f}min)\n")

    # ---- knob-bite proof BEFORE any result is read
    a = run("contract_vol_3x", 0.30, "dte21", store, sig,
            (date(2018, 1, 1), date(2018, 12, 31)))
    b = run("none", 0.30, "dte21", store, sig,
            (date(2018, 1, 1), date(2018, 12, 31)))
    bites = a and b and (a["n"], round(a["mean"], 4)) != (b["n"],
                                                         round(b["mean"], 4))
    print(f"KNOB PROOF trigger: signal n={a['n'] if a else 0} vs "
          f"control n={b['n'] if b else 0} -> "
          f"{'BITES' if bites else 'INERT — ABORT'}")
    if not bites:
        return 1

    print(f"\n=== TRAIN {TRAIN[0]}..{TRAIN[1]} (search, not evidence) ===")
    results = []
    for tr in TRIGGERS:
        for dlt in DELTAS:
            for ex in EXITS:
                r = run(tr, dlt, ex, store, sig, TRAIN, "pess")
                results.append({"trigger": tr, "delta": dlt, "exit": ex,
                                "train": r})
                show(f"{tr} d{dlt} {ex}", r)

    ctrl = {(x["delta"], x["exit"]): x["train"]
            for x in results if x["trigger"] == "none"}
    promoted = []
    for x in results:
        r = x["train"]
        if x["trigger"] == "none" or not r:
            continue
        c = ctrl.get((x["delta"], x["exit"]))
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
        v = run(x["trigger"], x["delta"], x["exit"], store, sig, VALID, "pess")
        cv = run("none", x["delta"], x["exit"], store, sig, VALID, "pess")
        x["validate"] = v
        show(f"{x['trigger']} d{x['delta']} {x['exit']}", v)
        if v and cv:
            lo, _ = boot_ci(v["pnls"])
            print(f"    control ${cv['mean']:.2f} -> "
                  f"{'HOLDS' if lo > 0 and v['mean'] > cv['mean'] else 'fails'}")

    out = DATA / "volsignal_study.json"
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
