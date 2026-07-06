"""
Earnings calendar fetcher.

Priority order:
  1. Finnhub free tier (60 req/min, 1 yr history of dates)
  2. yfinance Ticker.calendar fallback (less reliable but free, no API key)
  3. yfinance get_earnings_dates fallback

We CACHE per day to avoid hammering APIs. The cache is invalidated daily so
fresh earnings announcements get picked up Sunday-to-Sunday.

ALARM: if both Finnhub and yfinance return empty for a known-large ticker
that has been around for years (e.g., AAPL, MSFT), we WARN — that means
both data sources are broken and our earnings exclusion is silently failing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from csp_screener import config

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
CACHE_FILE = config.CACHE_DIR / "earnings_cache.json"
CACHE_TTL_HOURS = 18  # one trading day-ish


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Could not save earnings cache: {e}")


def _cache_fresh(entry: dict) -> bool:
    if "fetched_at" not in entry:
        return False
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
        return (datetime.now() - fetched) < timedelta(hours=CACHE_TTL_HOURS)
    except Exception:
        return False


def _finnhub_earnings(ticker: str, api_key: str) -> Optional[datetime]:
    """Fetch next earnings date from Finnhub. Returns None on any failure."""
    try:
        # Look 6 months out for next earnings
        today = datetime.now().date()
        end = today + timedelta(days=180)
        url = f"{FINNHUB_BASE}/calendar/earnings"
        params = {
            "from": today.isoformat(),
            "to": end.isoformat(),
            "symbol": ticker,
            "token": api_key,
        }
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            logger.debug(f"Finnhub {ticker} HTTP {resp.status_code}")
            return None
        data = resp.json()
        items = data.get("earningsCalendar") or []
        if not items:
            return None
        # Sort by date ascending, return the earliest future date
        items = sorted(items, key=lambda x: x.get("date", "9999-99-99"))
        for item in items:
            d = item.get("date")
            if not d:
                continue
            dt = datetime.fromisoformat(d)
            if dt.date() >= today:
                return dt
        return None
    except Exception as e:
        logger.debug(f"Finnhub fetch failed for {ticker}: {e}")
        return None


def _yfinance_earnings(ticker: str) -> Optional[datetime]:
    """Fetch next earnings date from yfinance. Returns None on any failure."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        # Try get_earnings_dates first (more reliable)
        try:
            ed = t.get_earnings_dates(limit=8)
            if ed is not None and not ed.empty:
                today = datetime.now()
                future = [
                    idx.to_pydatetime()
                    for idx in ed.index
                    if idx.to_pydatetime().replace(tzinfo=None) >= today
                ]
                if future:
                    return future[0].replace(tzinfo=None)
        except Exception:
            pass

        # Fallback: calendar attribute
        cal = t.calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed:
                if isinstance(ed, list) and ed:
                    return _coerce_date(ed[0])
                return _coerce_date(ed)
        else:
            # DataFrame fallback (older yfinance)
            try:
                ed = cal.T.iloc[0].get("Earnings Date")
                return _coerce_date(ed)
            except Exception:
                pass
        return None
    except Exception as e:
        logger.debug(f"yfinance earnings fetch failed for {ticker}: {e}")
        return None


def _coerce_date(d) -> Optional[datetime]:
    """Best-effort coercion to datetime."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.replace(tzinfo=None)
    try:
        import pandas as pd
        return pd.Timestamp(d).to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


def fetch_next_earnings(ticker: str) -> Optional[datetime]:
    """
    Returns next earnings datetime for ticker, or None if unknown.

    Tries cache → Finnhub → yfinance. Caches result for CACHE_TTL_HOURS.
    """
    cache = _load_cache()
    entry = cache.get(ticker, {})
    if _cache_fresh(entry):
        if entry.get("next_earnings"):
            return datetime.fromisoformat(entry["next_earnings"])
        return None

    next_earnings: Optional[datetime] = None
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if api_key:
        next_earnings = _finnhub_earnings(ticker, api_key)
        # Be a polite Finnhub citizen (free tier: 60/min)
        time.sleep(1.1)

    if next_earnings is None:
        next_earnings = _yfinance_earnings(ticker)

    cache[ticker] = {
        "next_earnings": next_earnings.isoformat() if next_earnings else None,
        "fetched_at": datetime.now().isoformat(),
    }
    _save_cache(cache)

    return next_earnings


def fetch_batch_earnings(tickers: list[str]) -> dict[str, Optional[datetime]]:
    """Fetch earnings for a batch. Sleeps respect Finnhub rate limit."""
    out: dict[str, Optional[datetime]] = {}
    for t in tickers:
        out[t] = fetch_next_earnings(t)
    return out


def fetch_ex_dividend(ticker: str) -> Optional[datetime]:
    """
    Next ex-dividend date via yfinance calendar (best-effort, cached daily).
    Used for the LIVE-tier ex-div blackout: a short put assigned across the
    ex-date costs an EU person 15% NRA withholding on the dividend
    (playbook change #10). Returns None when unknown.
    """
    cache = _load_cache()
    key = f"exdiv::{ticker}"
    entry = cache.get(key, {})
    if _cache_fresh(entry):
        if entry.get("ex_dividend"):
            return datetime.fromisoformat(entry["ex_dividend"])
        return None

    ex_div: Optional[datetime] = None
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        if isinstance(cal, dict):
            ex_div = _coerce_date(cal.get("Ex-Dividend Date"))
    except Exception as e:
        logger.debug(f"ex-div fetch failed for {ticker}: {e}")

    cache[key] = {
        "ex_dividend": ex_div.isoformat() if ex_div else None,
        "fetched_at": datetime.now().isoformat(),
    }
    _save_cache(cache)
    return ex_div


def sanity_check_data_sources() -> dict:
    """
    Run a smoke test against well-known tickers that must have upcoming earnings.
    If both data sources return None for AAPL/MSFT, something is broken.
    """
    canaries = ["AAPL", "MSFT", "AMZN"]
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    results = {"finnhub_available": bool(api_key), "details": {}}
    for c in canaries:
        finnhub_dt = _finnhub_earnings(c, api_key) if api_key else None
        yf_dt = _yfinance_earnings(c)
        results["details"][c] = {
            "finnhub": finnhub_dt.isoformat() if finnhub_dt else None,
            "yfinance": yf_dt.isoformat() if yf_dt else None,
        }
    # Alarm: any canary where BOTH sources returned None
    broken = [c for c, d in results["details"].items()
              if not d["finnhub"] and not d["yfinance"]]
    results["broken"] = broken
    results["healthy"] = len(broken) == 0
    return results
