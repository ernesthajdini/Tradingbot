"""
FULL-STUDY PHASE 3c — rank-first candidate precomputation.

WHY: the blind options pull for all 5,506 members-ever measured ~38 DAYS at
the vendor's option-endpoint serving rate. But the daily RANKING is a pure
function of stock data (filters: band/volume/earnings; ranker: RV
percentile) — and the stock data is fully downloaded. So we replay the
production filter+rank pipeline over every trading day 2016→2026 first,
and pull options ONLY for the daily top-N each tier — the only names whose
chains the engine will ever request. Zero fidelity loss; ~95% less data.

EXACTNESS CONTRACT: this must reproduce engine/ranker decisions bit-for-
bit — same RV window/percentile math (ranker.compute_rv_percentile), same
filters (band, MIN_DAILY_VOLUME via 20d avg, earnings blackout from the
earnings/ dir), same tie-breaks (sort by -pct, -rv, stable over
alphabetical ticker order). The study runner verifies engine-vs-precompute
agreement and refuses to proceed on divergence.

Output: data/thetadata_full/candidates.json
  {"days": {"YYYY-MM-DD": {"sandbox": [...], "live": [...]}},
   "params": {...}}
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd

from csp_screener import config

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
TOP_N = config.MAX_CANDIDATES_IN_EMAIL
BANDS = {
    "sandbox": (config.PRICE_MIN, config.PRICE_MAX),
    "live": (config.LIVE_PRICE_MIN, config.LIVE_PRICE_MAX),
}


def load_earnings() -> dict[str, list[date]]:
    out = {}
    edir = DATA / "earnings"
    if not edir.exists():
        return out
    for f in edir.glob("*.csv"):
        try:
            out[f.stem] = sorted(
                date.fromisoformat(l.strip())
                for l in f.read_text(encoding="utf-8").splitlines()
                if l.strip())
        except ValueError:
            continue
    return out


def ticker_daily(csv_path: Path, earnings: list[date] | None):
    """Per-ticker daily frame with the exact production metrics:
    rv percentile (ranker math), 20d avg volume, earnings-blackout flag."""
    # Load through the SAME loader the engine uses, so zero-close cleaning
    # and split adjustment are identical on both sides of the verification.
    # The ranker prices RV off "Adj Close" when present (production does the
    # same), while the band filter uses the as-traded close.
    from csp_screener.backtest import data_loader
    src = data_loader.load_thetadata_stock(csv_path)
    if len(src) < config.RV_WINDOW_DAYS + 10:
        return None
    out = pd.DataFrame({
        "close": src["Close"].to_numpy(),
        "adj": (src["Adj Close"] if "Adj Close" in src.columns
                else src["Close"]).to_numpy(),
        "volume": src["Volume"].to_numpy(),
    }, index=src.index)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if len(out) < config.RV_WINDOW_DAYS + 10:
        return None

    # EXACT production semantics (ranker.compute_rv_percentile):
    #   rv_series = rolling-std annualized, THEN .dropna()
    #   current   = rv_series.iloc[-1]     <- last VALID value
    #   history   = rv_series.iloc[-252:]  <- last 252 VALID values
    # The dropna matters: it compresses out invalid windows, so both the
    # "current" RV and the percentile denominator come from valid values
    # only. Ranking over raw row positions instead diverges whenever a
    # ticker has a gap (verified: AKAO's real denominator was 63, not 252).
    W, H = config.RV_WINDOW_DAYS, config.RV_HISTORY_DAYS
    log_ret = np.log(out["adj"] / out["adj"].shift(1))
    rv_raw = log_ret.rolling(W).std() * np.sqrt(252)
    rvv = rv_raw.replace([np.inf, -np.inf], np.nan).dropna()
    if rvv.empty:
        return None
    # method="max" reproduces (history <= current).sum(): tied values take
    # the HIGHEST rank. The default "average" splits ties and diverges.
    pctv = rvv.rolling(H, min_periods=1).rank(method="max", pct=True) * 100
    flat = (rvv.rolling(H, min_periods=1).max()
            == rvv.rolling(H, min_periods=1).min())
    pctv[flat] = 50.0
    # Re-expand onto every calendar row: a day inherits the last valid
    # percentile, exactly what iloc[-1] on the dropna'd series returns when
    # the newest windows are invalid.
    out["rv"] = rvv.reindex(out.index).ffill()
    pct = pctv.reindex(out.index).ffill()
    # compute_rv_percentile returns NaN until len(log_ret) >= window + 5.
    pct.iloc[:W + 5] = np.nan
    out["pct"] = pct
    out["avg20"] = out["volume"].rolling(20).mean()

    # Earnings blackout: True when next earnings within EARNINGS_EXCLUSION_DAYS
    blocked = np.zeros(len(out), dtype=bool)
    if earnings:
        edates = np.array([np.datetime64(e) for e in earnings])
        days = out.index.values.astype("datetime64[D]")
        idx = np.searchsorted(edates, days)
        has_next = idx < len(edates)
        gap = np.full(len(out), 10_000)
        gap[has_next] = (edates[idx[has_next]] - days[has_next]).astype(int)
        blocked = (gap >= 0) & (gap <= config.EARNINGS_EXCLUSION_DAYS)
    out["earn_blocked"] = blocked
    return out


def main() -> int:
    t0 = time.time()
    earnings = load_earnings()
    stocks = sorted((DATA / "stocks").glob("*.csv"))
    print(f"{len(stocks)} histories, {len(earnings)} earnings calendars")

    # day -> tier -> list of (pct, rv, ticker)
    per_day: dict = {}
    for i, p in enumerate(sorted(stocks), 1):
        sym = p.stem
        td = ticker_daily(p, earnings.get(sym))
        if td is None:
            continue
        ok = td[td["pct"].notna() & (td["avg20"] >= config.MIN_DAILY_VOLUME)
                & ~td["earn_blocked"]]
        for tier, (lo, hi) in BANDS.items():
            sel = ok[(ok["close"] >= lo) & (ok["close"] <= hi)]
            for ts, row in sel.iterrows():
                day = ts.date().isoformat()
                per_day.setdefault(day, {}).setdefault(tier, []).append(
                    (row["pct"], row["rv"], sym))
        if i % 2000 == 0:
            print(f"  {i}/{len(stocks)} ({time.time()-t0:.0f}s)")

    days = {}
    for day, tiers in per_day.items():
        days[day] = {}
        for tier, rows in tiers.items():
            # Exact ranker order: -pct, -rv, stable over alphabetical input
            rows.sort(key=lambda r: (-r[0], -r[1]))
            days[day][tier] = [sym for _, _, sym in rows[:TOP_N]]

    payload = {
        "generated": date.today().isoformat(),
        "params": {"top_n": TOP_N, "bands": BANDS,
                   "min_volume": config.MIN_DAILY_VOLUME,
                   "earnings_exclusion_days": config.EARNINGS_EXCLUSION_DAYS,
                   "earnings_coverage": len(earnings)},
        "days": dict(sorted(days.items())),
    }
    out = DATA / "candidates.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    uniq = {s for d in days.values() for t in d.values() for s in t}
    print(f"DONE in {(time.time()-t0)/60:.1f}min -> {out}")
    print(f"{len(days)} trading days, {len(uniq)} unique candidate tickers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
