"""
AMENDMENT 5 — call spreads and iron condors on the index universe.

STAMPED HONESTY COST (MANIFEST Amendment 5 section B): production's
setup_generator builds PUT structures only, so the call and condor logic
here is NEW backtest-only code. It deliberately mirrors the production
gates — same friction model, same net-at-designed-exit credit floor, same
OI and spread-width gates, never-sell-ITM applied to both wings, same
sanity caps, same 50% take-profit / 21-DTE / -2x exit rules — but it is a
mirror, not the original. Results are stamped generator=backtest_mirror,
and porting any survivor to production is a rewrite, not a copy.

The arithmetic that makes the condor worth testing: a second credit from
the call side against a max loss that stays one-sided (only one wing can
finish in the money), so credit roughly doubles while risk does not.
Friction doubles too — but against a doubled credit, where every prior
failure came from friction eating a credit that could not grow.

    python csp_screener/backtest/condor_study.py
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

from csp_screener import config
from csp_screener.backtest import data_loader
from csp_screener.backtest.day_store import DayStore
from csp_screener.virtual_tracker import close_economics

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
STORE = DATA / "daystore_condor"
TRAIN = (date(2017, 2, 8), date(2021, 12, 31))
VALID = (date(2022, 1, 1), date(2023, 12, 31))
STRUCTURES = ["put_spread", "call_spread", "iron_condor"]
SCALES = [(5.0, 400.0), (10.0, 800.0)]
DELTAS = [0.30, 0.20]
MAX_SHORT_DELTA = 0.40          # mirrors setup_generator.MAX_CSP_DELTA
TP = config.VIRTUAL_TP_PCT
EXIT_DTE = config.VIRTUAL_FORCE_EXIT_DTE
STOP_MULT = config.VIRTUAL_SL_MULTIPLE


def legs_round_trip(structure):
    return 8 if structure == "iron_condor" else 4


def friction(credit, exit_price, structure, band="pess"):
    slip = (config.SLIPPAGE_PCT_PESSIMISTIC if band == "pess"
            else config.SLIPPAGE_PCT_OF_PREMIUM)
    return (legs_round_trip(structure) * config.COMMISSION_PER_CONTRACT
            + slip * (credit + exit_price))


def net_at_tp(credit, structure):
    """Production's net_at_tp_exit, generalised to leg count."""
    exit_price = (1.0 - TP) * credit
    return TP * credit - friction(credit, exit_price, structure, band="base")


def pick_wing(rows, spot, want_delta, width, side):
    """One vertical: short leg nearest `want_delta`, long leg `width` away.
    Mirrors _pick_best_put's gates on both sides."""
    if side == "put":
        cands = rows[(rows["right"] == "P") & (rows["strike"] < spot)]
    else:
        cands = rows[(rows["right"] == "C") & (rows["strike"] > spot)]
    cands = cands[(cands["bid"] > 0) & (cands["ask"] > 0)]
    if cands.empty:
        return None
    mid = (cands["bid"] + cands["ask"]) / 2
    cands = cands.assign(mid=mid)
    cands = cands[cands["mid"] > 0]
    width_pct = (cands["ask"] - cands["bid"]) / cands["mid"]
    shorts = cands[(width_pct <= config.MAX_BID_ASK_PCT_OF_MID)
                   & (cands["delta"].abs() <= MAX_SHORT_DELTA)]
    oi_known = (cands["open_interest"] > 0).any()
    if oi_known:
        shorts = shorts[shorts["open_interest"] >= config.MIN_OPEN_INTEREST]
    shorts = shorts[shorts["delta"].notna()]
    if shorts.empty:
        return None
    short = shorts.iloc[(shorts["delta"].abs() - want_delta).abs().argsort()[:1]]
    if short.empty:
        return None
    short = short.iloc[0]
    tgt = short["strike"] - width if side == "put" else short["strike"] + width
    longs = cands[(cands["strike"] < short["strike"]) if side == "put"
                  else (cands["strike"] > short["strike"])]
    # Long wing: absolute-cents gate, as production does (cheap wings always
    # look wide in percentage terms).
    longs = longs[(longs["ask"] - longs["bid"])
                  <= np.maximum(0.05, 0.10 * longs["mid"])]
    if oi_known:
        longs = longs[longs["open_interest"] >= config.MIN_OPEN_INTEREST]
    if longs.empty:
        return None
    lg = longs.iloc[(longs["strike"] - tgt).abs().argsort()[:1]].iloc[0]
    w = abs(short["strike"] - lg["strike"])
    if w <= 0:
        return None
    credit = (short["mid"] - lg["mid"]) * 100.0
    if credit <= 0:
        return None
    return {"short": float(short["strike"]), "long": float(lg["strike"]),
            "width": float(w), "credit": float(credit),
            "delta": float(short["delta"])}


def value_of(rows, wing, side):
    """Current value of a vertical from the day's quotes, or None."""
    def leg(strike, tight):
        r = rows[(rows["right"] == ("P" if side == "put" else "C"))
                 & (rows["strike"].round(4) == round(strike, 4))]
        if r.empty:
            return None
        r = r.iloc[0]
        b, a = float(r["bid"]), float(r["ask"])
        if not (b > 0 and a > 0):
            return None
        m = (b + a) / 2
        if m <= 0:
            return None
        if tight and (a - b) / m > config.MAX_BID_ASK_PCT_OF_MID * 2:
            return None
        return m
    s = leg(wing["short"], True)
    if s is None:
        return None
    l = leg(wing["long"], False)
    if l is None:
        return None
    v = (s - l) * 100.0
    return v if v >= 0 else None



def spot_at(prices, ticker, asof):
    px = prices.get(ticker)
    if px is None:
        return None
    h = px[px.index.date <= asof]
    if h.empty:
        return None
    return float(h["Close"].iloc[-1])


def intrinsic_value(pos, spot):
    """What the structure is actually worth, per contract, ignoring time
    value. A short vertical is worth what it costs to close: the amount the
    short leg is in the money, capped at the width."""
    total = 0.0
    for side, w in pos["wings"].items():
        if side == "put":
            itm = max(0.0, w["short"] - spot)
        else:
            itm = max(0.0, spot - w["short"])
        total += min(itm, w["width"]) * 100.0
    return total


def run(structure, scale, delta, store, cand, prices, window):
    width, max_risk = scale
    dates = [d for d in store.dates if window[0] <= d <= window[1]]
    book, trades, unsettled = [], [], []
    cooldown = {}
    for asof in dates:
        day = store.day(asof)
        if day.empty:
            continue
        # ---- exits
        for pos in list(book):
            rows = day[day["ticker"] == pos["ticker"]]
            rows = rows[rows["expiration"] == pos["expiration"]]
            dte = (pos["expiration"] - asof).days
            val = None
            if not rows.empty:
                parts = []
                ok = True
                for side, wing in pos["wings"].items():
                    v = value_of(rows, wing, side)
                    if v is None:
                        ok = False
                        break
                    parts.append(v)
                val = sum(parts) if ok else None
            unmarked = val is None
            if val is None:
                # BUG FOUND AND FIXED (this cost the first condor result):
                # this branch used to book val = 0.0 at expiry — "expired
                # worthless, keep the whole credit". But a position becomes
                # UNMARKABLE precisely when it goes against you: the ITM leg's
                # spread blows out and the mark gate rejects it. So every
                # maximum loss was being recorded as a maximum win. It hit
                # 64.5% of condor trades at an average of +$146, manufacturing
                # the entire "finding". Settle at INTRINSIC instead, which is
                # what the position is actually worth at expiry.
                spot_now = spot_at(prices, pos["ticker"], asof)
                if spot_now is None:
                    if dte > 0:
                        continue
                    # No spot to settle against: drop the position from the
                    # record rather than invent a value for it, and count it.
                    book.remove(pos)
                    unsettled.append(pos)
                    continue
                val = intrinsic_value(pos, spot_now)
                if dte > 0 and not (
                        (pos["credit"] - val) <= -STOP_MULT * pos["credit"]
                        or dte <= EXIT_DTE):
                    continue          # unmarkable but not yet due to exit
            pnl_pct = (pos["credit"] - val) / pos["credit"]
            hit = (pnl_pct >= TP or dte <= EXIT_DTE
                   or (pos["credit"] - val) <= -STOP_MULT * pos["credit"])
            if not hit:
                continue
            econ_b = close_economics(pos["credit"], val, "csp")
            gross = pos["credit"] - val
            trades.append({
                "exit": ("expiry_unmarked" if unmarked else
                         "tp" if pnl_pct >= TP else
                         "dte" if dte <= EXIT_DTE else "stop"),
                "dte_at_exit": dte,
                "ticker": pos["ticker"], "opened": pos["opened"].isoformat(),
                "closed": asof.isoformat(), "credit": pos["credit"],
                "exit_value": val, "gross": gross,
                "pnl": gross - friction(pos["credit"], val, structure, "base"),
                "pnl_pess": gross - friction(pos["credit"], val, structure, "pess"),
                "risk": pos["risk"],
            })
            book.remove(pos)
            cooldown[pos["ticker"]] = asof + timedelta(days=3)

        # ---- entries
        uni = cand.get(asof.isoformat(), {}).get("live", [])
        open_t = {p["ticker"] for p in book}
        for tk in uni:
            if len(book) >= config.MAX_VIRTUAL_OPEN or tk in open_t:
                continue
            if cooldown.get(tk) and asof <= cooldown[tk]:
                continue
            px = prices.get(tk)
            if px is None:
                continue
            h = px[px.index.date <= asof]
            if h.empty or (asof - h.index[-1].date()).days > 0:
                continue
            spot = float(h["Close"].iloc[-1])
            rows = day[day["ticker"] == tk]
            if rows.empty:
                continue
            for exp in sorted(rows["expiration"].unique()):
                dte = (exp - asof).days
                if not (config.DTE_MIN <= dte <= config.DTE_MAX):
                    continue
                er = rows[rows["expiration"] == exp]
                wings, credit = {}, 0.0
                sides = ({"put"} if structure == "put_spread"
                         else {"call"} if structure == "call_spread"
                         else {"put", "call"})
                ok = True
                for side in sides:
                    w = pick_wing(er, spot, delta, width, side)
                    if w is None:
                        ok = False
                        break
                    wings[side] = w
                    credit += w["credit"]
                if not ok:
                    continue
                # Max loss is ONE-SIDED for a condor: only one wing can
                # finish in the money.
                risk = max(w["width"] for w in wings.values()) * 100.0 - credit
                if risk > max_risk or risk <= 0:
                    continue
                if net_at_tp(credit, structure) < config.MIN_NET_CREDIT_AFTER_FRICTION:
                    continue
                book.append({"ticker": tk, "expiration": exp, "opened": asof,
                             "wings": wings, "credit": credit, "risk": risk})
                open_t.add(tk)
                break
    if not trades:
        return None
    a = np.array([t["pnl_pess"] for t in trades])
    b = np.array([t["pnl"] for t in trades])
    worst = abs(a.min()) / abs(a.sum()) if a.sum() else float("inf")
    from collections import Counter
    exits = Counter(t["exit"] for t in trades)
    by_exit = {k: (len([t for t in trades if t["exit"] == k]),
                   round(float(np.mean([t["pnl_pess"] for t in trades
                                        if t["exit"] == k])), 2))
               for k in exits}
    return {"exits": dict(exits), "by_exit": by_exit,
            "unsettled": len(unsettled),
            "n": len(a), "mean_pess": float(a.mean()), "mean_base": float(b.mean()),
            "total_pess": float(a.sum()), "median_pess": float(np.median(a)),
            "win": float((a > 0).mean()), "worst_share": float(worst),
            "avg_credit": float(np.mean([t["credit"] for t in trades])),
            "avg_risk": float(np.mean([t["risk"] for t in trades])),
            "pnls": [float(x) for x in a]}


def boot_ci(vals, iters=10000, seed=17):
    a = np.asarray(vals, dtype=float)
    rng = np.random.default_rng(seed)
    bs = np.array([rng.choice(a, len(a), replace=True).mean()
                   for _ in range(iters)])
    return float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def main() -> int:
    t0 = time.time()
    store = DayStore(STORE)
    cand = json.loads((DATA / "candidates_index.json").read_text(
        encoding="utf-8"))["days"]
    prices = {}
    for d in sorted(p for p in (DATA / "options_index").iterdir() if p.is_dir()):
        f = d / "stock_eod.csv"
        if f.exists():
            try:
                prices[d.name] = data_loader.load_thetadata_stock(f)
            except Exception:
                pass
    print(f"condor store: {len(store.dates)} days, {len(prices)} underlyings")
    print("generator: backtest_mirror (NOT production code — Amendment 5B)\n",
          flush=True)

    rows = []
    for st in STRUCTURES:
        for sc in SCALES:
            for dl in DELTAS:
                r = run(st, sc, dl, store, cand, prices, TRAIN)
                label = f"{st:12} w${sc[0]:.0f} risk${sc[1]:.0f} d{dl:.2f}"
                if r is None:
                    print(f"  {label}  no trades")
                    rows.append({"structure": st, "scale": list(sc),
                                 "delta": dl, "train": None})
                    continue
                rows.append({"structure": st, "scale": list(sc), "delta": dl,
                             "train": {k: v for k, v in r.items() if k != "pnls"}})
                print(f"  {label}  n={r['n']:4} win={r['win']:.0%} "
                      f"per-trade [{r['mean_pess']:7.2f},{r['mean_base']:7.2f}] "
                      f"credit=${r['avg_credit']:.0f} risk=${r['avg_risk']:.0f}",
                      flush=True)

    promoted = [r for r in rows if r["train"] and r["train"]["n"] >= 100
                and r["train"]["mean_pess"] > 0
                and r["train"]["median_pess"] > 0
                and r["train"]["worst_share"] <= 0.40]
    print(f"\nTRAIN complete ({(time.time()-t0)/60:.1f}min): "
          f"{len(rows)} configs, {len(promoted)} promoted")
    if not promoted:
        print("  none — no structure met the pre-registered promotion bar")
    for r in promoted:
        v = run(r["structure"], tuple(r["scale"]), r["delta"], store, cand,
                prices, VALID)
        if not v:
            print(f"  VALIDATE {r['structure']}: no trades")
            continue
        lo, hi = boot_ci(v["pnls"])
        print(f"  VALIDATE {r['structure']:12} n={v['n']:4} "
              f"per-trade={v['mean_pess']:7.2f} CI[{lo:7.2f},{hi:7.2f}] "
              f"-> {'FINDING' if lo > 0 else 'fails validation'}")
        r["validate"] = {k: val for k, val in v.items() if k != "pnls"}

    out = DATA / "condor_study.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(),
                               "generator": "backtest_mirror",
                               "configs": rows}, indent=1, default=str),
                   encoding="utf-8")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
