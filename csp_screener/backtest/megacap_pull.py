"""
AMENDMENT 16 DATA — the mega-cap / high-liquidity option tier.

The existing archive was pulled from the screener's own candidate band
($5-60 stocks), so it contains NO mega-cap. Its most liquid decile still
quotes $0.10 on a $0.70 mid — a 17.1% round-trip toll. The tier where the
world's option volume actually sits has never been measured here.

UNIVERSE IS FROZEN IN THIS FILE, declared before any result is read, chosen
by option-market liquidity (the most heavily traded US single-name option
underlyings) and deliberately spanning price tiers so the toll can be plotted
against underlying price rather than asserted.

Output: data/thetadata_full/options_mega/<TICKER>/{puts,calls,oi,oi_calls}_<EXP>.csv
Its OWN tree — the existing archive is never modified. Resumable per ticker,
crash-isolated, tier walls fatal.

    python csp_screener/backtest/megacap_pull.py
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from csp_screener.backtest.calls_pull import DATA, WORKERS, _get

OUT = DATA / "options_mega"
START, END = date(2019, 1, 1), date(2023, 12, 31)
ACTIVE_DAYS = 70          # entry at <=45 DTE + marking to expiry, with margin

# Frozen universe — 40 of the most heavily traded US single-name options,
# spanning ~$10 to ~$600 so the toll can be measured AGAINST price.
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX",
    "AVGO", "CRM", "ADBE", "COST", "WMT", "JPM", "BAC", "GS", "XOM", "CVX",
    "BA", "CAT", "DIS", "UBER", "PYPL", "COIN", "MU", "INTC", "QCOM", "ORCL",
    "IBM", "LLY", "UNH", "JNJ", "PFE", "SMCI", "PLTR", "MRVL", "LRCX", "TSM",
    "BABA",
]

# SCOPE CUT 2026-09-21, deadline-driven, NOT a change to the declared space:
# Amendment 16 tests short puts and short PUT spreads only, so the call legs
# were never needed by it. Dropping them halves the requests and is what
# makes the 40-ticker universe reachable before the subscription lapses on
# ~25 Sep. Calls already pulled (AAPL/MSFT/NVDA/AMZN) stay on disk.
ENDPOINTS = [("puts", "/v3/option/history/eod", "put"),
             ("oi", "/v3/option/history/open_interest", "put")]


def pull_ticker(sym: str) -> dict:
    tdir = OUT / sym
    tdir.mkdir(parents=True, exist_ok=True)
    marker = tdir / ".complete_mega"
    counts = {"files": 0, "skip": 0, "empty": 0, "fail": 0}
    if marker.exists():
        counts["skip"] = 1
        return counts

    text = _get("/v3/option/list/expirations", {"symbol": sym}, sym)
    if text is None:
        # HARD FAILURE (terminal down/slow) — must NOT be recorded as done.
        # The first version touched the marker here, so ~30 tickers were
        # silently marked COMPLETE with zero data while the terminal was
        # restarting, and were then skipped forever.
        counts["fail"] += 1
        return counts
    if not text:
        marker.touch()          # genuine empty: this symbol has no expirations
        return counts
    exps = []
    for line in text.splitlines()[1:]:
        parts = line.replace('"', "").split(",")
        if len(parts) >= 2:
            try:
                e = date.fromisoformat(parts[1])
                if START <= e <= END:
                    exps.append(e)
            except ValueError:
                pass

    for exp in sorted(set(exps)):
        lo = max(exp - timedelta(days=ACTIVE_DAYS), START)
        hi = min(exp, END)
        if lo > hi:
            continue
        for kind, path, right in ENDPOINTS:
            out = tdir / f"{kind}_{exp.isoformat()}.csv"
            if out.exists():
                counts["skip"] += 1
                continue
            text = _get(path, {"symbol": sym, "expiration": exp.isoformat(),
                               "right": right, "start_date": lo.isoformat(),
                               "end_date": hi.isoformat()}, sym)
            if text is None:
                counts["fail"] += 1
            elif text and len(text.splitlines()) > 1:
                out.write_text(text, encoding="utf-8")
                counts["files"] += 1
            else:
                out.write_text("", encoding="utf-8")       # tried, no data
                counts["empty"] += 1
    if counts["fail"] == 0:
        marker.touch()
    return counts


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    log = open(DATA / "megacap_pull.log", "a", encoding="utf-8", buffering=1)
    print(f"{len(UNIVERSE)} frozen tickers, {START}..{END}", file=log)
    totals = {"files": 0, "skip": 0, "empty": 0, "fail": 0, "crashed": 0}
    t0, done = time.time(), 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(pull_ticker, t): t for t in UNIVERSE}
        for fut in as_completed(futs):
            try:
                c = fut.result()
            except RuntimeError:
                raise                                   # tier wall — stop
            except Exception as e:
                totals["crashed"] += 1
                print(f"CRASH {futs[fut]}: {e!r}", file=log)
                c = {}
            for k in totals:
                totals[k] += c.get(k, 0)
            done += 1
            print(f"{done}/{len(UNIVERSE)} tickers ({(time.time()-t0)/3600:.1f}h) "
                  f"{totals}", file=log)
    print(f"MEGACAP PULL DONE in {(time.time()-t0)/3600:.1f}h: {totals}", file=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
