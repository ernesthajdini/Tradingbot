"""
Daily candidate list for the index_etf universe (MANIFEST Amendment 1 + E).

Same ranker as production (RV percentile on split-adjusted closes, exact
compute_rv_percentile semantics via the shared precompute helper), same
volume and earnings gates, NO price band — see Amendment 1 section E: for a
defined-risk spread, max loss is width-based and independent of the
underlying's price, so the cash-secured band does not apply.
"""
from __future__ import annotations
import json, sys, time
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from csp_screener import config
from csp_screener.backtest import precompute_candidates as pc

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
SRC = DATA / "options_index"
TOP_N = config.MAX_CANDIDATES_IN_EMAIL


def main() -> int:
    t0 = time.time()
    earnings = pc.load_earnings()
    per_day: dict = {}
    tickers = sorted(p.name for p in SRC.iterdir() if p.is_dir())
    kept = 0
    for sym in tickers:
        f = SRC / sym / "stock_eod.csv"
        if not f.exists():
            continue
        td = pc.ticker_daily(f, earnings.get(sym))
        if td is None:
            continue
        kept += 1
        ok = td[td["pct"].notna()
                & (td["avg20"] >= config.MIN_DAILY_VOLUME)
                & ~td["earn_blocked"]]
        for ts, row in ok.iterrows():
            per_day.setdefault(ts.date().isoformat(), {}).setdefault(
                "live", []).append((row["pct"], row["rv"], sym))
    days = {}
    for day, tiers in per_day.items():
        days[day] = {}
        for tier, rows in tiers.items():
            rows.sort(key=lambda r: (-r[0], -r[1]))
            days[day][tier] = [s for _, _, s in rows[:TOP_N]]
    out = DATA / "candidates_index.json"
    out.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "params": {"top_n": TOP_N, "band": "none (Amendment 1 section E)",
                   "min_volume": config.MIN_DAILY_VOLUME},
        "days": dict(sorted(days.items()))}), encoding="utf-8")
    uniq = {s for d in days.values() for t in d.values() for s in t}
    print(f"DONE in {time.time()-t0:.0f}s: {kept} ETFs with history, "
          f"{len(days)} trading days, {len(uniq)} ever ranked -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
