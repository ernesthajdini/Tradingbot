"""
ThetaData v3 downloader — pulls EOD put chains + stock EOD into local CSVs
under backtest/data/thetadata/ (gitignored), for the walk-forward engine.

Requires the Theta Terminal running locally (see PILOT_CHECKLIST.md):
    cd csp_screener/backtest/tools
    ./jdk-21.0.12+8-jre/bin/java.exe -jar ThetaTerminalv3.jar --creds-file creds.txt
The terminal serves http://127.0.0.1:25503 (v3 API). Verified 2026-08-07:
FREE tier serves options EOD from 2023-06-01, INCLUDING delisted
underlyings (FSR confirmed); VALUE from 2020-01-01; STANDARD from
2016-01-01 (fixed first-access dates, not rolling). Open interest is
VALUE-gated — on FREE the chains carry OI=0, which the production gates
treat as OI-UNKNOWN (accept with warning), matching their design.

Request shape: one GET per (ticker, expiration, right=put, date-range)
returns every strike for every day — so a ticker-window pull is
len(expirations) requests, not len(strikes) x len(days). The free tier
allows 1 concurrent request; PACE_SECONDS keeps us polite.

Resumable: each (ticker, expiration) lands in its own CSV; existing
non-empty files are skipped on re-run.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import requests

BASE = "http://127.0.0.1:25503"
DATA_DIR = Path(__file__).resolve().parent / "data" / "thetadata"
PACE_SECONDS = 0.30          # polite gap between requests (free tier: 1 concurrent)
RETRY_BACKOFF = [5, 15, 60]  # on 429/5xx/timeouts
# Free-tier serving is SLOW for big ranges — a 120s client timeout aborted
# streams mid-response (jetty broken-pipe stacktraces in terminal.log) and
# the retries re-requested the same heavy payloads. Long timeout + trimmed
# per-expiration windows keep each response small instead.
REQUEST_TIMEOUT = 600
# An expiration only quotes meaningfully in its final ~10 weeks; requesting
# the whole study range per expiration multiplied response sizes for rows
# that are empty anyway.
EXPIRY_ACTIVE_DAYS = 70

logger = logging.getLogger("thetadata_pull")


def _get(path: str, params: dict) -> str:
    """GET with pacing + backoff. Returns response text; raises on hard fail."""
    url = f"{BASE}{path}"
    for attempt, backoff in enumerate([0] + RETRY_BACKOFF):
        if backoff:
            logger.info(f"  backoff {backoff}s (attempt {attempt})")
            time.sleep(backoff)
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.warning(f"  request error: {e}")
            continue
        time.sleep(PACE_SECONDS)
        text = r.text
        if r.status_code == 200 and not text.lstrip().startswith("<"):
            # Tier-wall answers come back 4xx/plain-text; surface them loudly
            if "subscription" in text[:200].lower():
                raise PermissionError(f"tier wall: {text[:160]}")
            return text
        if r.status_code in (429, 500, 502, 503, 504):
            continue
        raise RuntimeError(f"{r.status_code}: {text[:160]}")
    raise RuntimeError(f"exhausted retries for {path} {params}")


def list_put_expirations(ticker: str, start: date, end: date,
                         dte_max: int = 45) -> list[date]:
    """Expirations that could hold a position opened inside [start, end]:
    anything expiring between start and end + dte_max."""
    text = _get("/v3/option/list/expirations", {"symbol": ticker})
    out = []
    for line in text.splitlines()[1:]:
        parts = line.replace('"', "").split(",")
        if len(parts) < 2:
            continue
        try:
            exp = date.fromisoformat(parts[1])
        except ValueError:
            continue
        if start <= exp <= end + timedelta(days=dte_max):
            out.append(exp)
    return sorted(set(out))


def pull_ticker(ticker: str, start: date, end: date) -> dict:
    """Pull stock EOD + all in-window put expirations for one ticker."""
    tdir = DATA_DIR / ticker
    tdir.mkdir(parents=True, exist_ok=True)
    summary = {"ticker": ticker, "expirations": 0, "skipped": 0, "stock": False}

    stock_path = tdir / "stock_eod.csv"
    if not (stock_path.exists() and stock_path.stat().st_size > 100):
        text = _get("/v3/stock/history/eod", {
            "symbol": ticker,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
        })
        stock_path.write_text(text, encoding="utf-8")
    summary["stock"] = True

    for exp in list_put_expirations(ticker, start, end):
        out = tdir / f"puts_{exp.isoformat()}.csv"
        if out.exists() and out.stat().st_size > 100:
            summary["skipped"] += 1
            continue
        # Trim the request to the expiry's active window — smaller payloads,
        # no wasted rows, no mid-stream timeouts.
        req_start = max(start, exp - timedelta(days=EXPIRY_ACTIVE_DAYS))
        req_end = min(end, exp)
        if req_start > req_end:
            continue
        try:
            text = _get("/v3/option/history/eod", {
                "symbol": ticker, "expiration": exp.isoformat(),
                "right": "put",
                "start_date": req_start.isoformat(),
                "end_date": req_end.isoformat(),
            })
        except (PermissionError, RuntimeError) as e:
            logger.warning(f"  {ticker} {exp}: {e}")
            continue
        out.write_text(text, encoding="utf-8")
        summary["expirations"] += 1
    logger.info(f"{ticker}: {summary['expirations']} expirations pulled, "
                f"{summary['skipped']} already present")
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Pull ThetaData EOD puts + stock")
    ap.add_argument("--tickers", required=True,
                    help="comma-separated, e.g. SNAP,T,FUBO")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    # Sanity: terminal reachable?
    try:
        requests.get(f"{BASE}/v3/option/list/expirations",
                     params={"symbol": "AAPL"}, timeout=10)
    except requests.RequestException:
        print("Theta Terminal is not running on :25503 — see PILOT_CHECKLIST.md")
        return 1

    t0 = time.time()
    for ticker in [t.strip().upper() for t in args.tickers.split(",") if t.strip()]:
        pull_ticker(ticker, start, end)
    print(f"Done in {time.time() - t0:.0f}s → {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
