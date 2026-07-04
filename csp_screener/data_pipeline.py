"""
Price/volume data fetcher built on yfinance.

We keep this small and dependency-light. The existing TradingAgent has a
fancier pipeline but it's not used here — quarantined per the architecture
review.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from csp_screener import config

logger = logging.getLogger(__name__)

_PRICE_CACHE: dict[str, pd.DataFrame] = {}
_CACHE_TIMESTAMP: Optional[datetime] = None
_CACHE_TTL_HOURS = 12


def fetch_prices(tickers: list[str], lookback_days: int = 400) -> dict[str, pd.DataFrame]:
    """
    Batch-download daily prices for a list of tickers from yfinance.
    Returns {ticker: DataFrame[Open, High, Low, Close, Adj Close, Volume]}.

    Cached in-process for CACHE_TTL_HOURS.
    """
    global _CACHE_TIMESTAMP

    now = datetime.now()
    if _CACHE_TIMESTAMP and (now - _CACHE_TIMESTAMP) < timedelta(hours=_CACHE_TTL_HOURS):
        cached = {t: df for t, df in _PRICE_CACHE.items() if t in tickers}
        missing = [t for t in tickers if t not in _PRICE_CACHE]
        if not missing:
            return cached
        # Fetch only missing
        tickers_to_fetch = missing
    else:
        _PRICE_CACHE.clear()
        tickers_to_fetch = tickers

    start = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    data = _download_with_retry(
        tickers_to_fetch, start=start, end=end,
        group_by="ticker", auto_adjust=False, threads=True,
    )
    if data is None:
        return {t: df for t, df in _PRICE_CACHE.items() if t in tickers}

    out: dict[str, pd.DataFrame] = {}
    if len(tickers_to_fetch) == 1:
        ticker = tickers_to_fetch[0]
        df = _extract_single_ticker(data, ticker)
        if df is not None and not df.empty:
            out[ticker] = df
    else:
        if not data.empty:
            for t in tickers_to_fetch:
                try:
                    df = data[t].dropna(subset=["Close"]).copy()
                    if not df.empty:
                        out[t] = df
                except Exception:
                    continue

    _PRICE_CACHE.update(out)
    _CACHE_TIMESTAMP = now
    return {t: df for t, df in _PRICE_CACHE.items() if t in tickers}


def _download_with_retry(tickers, attempts: int = 2, **kwargs) -> Optional[pd.DataFrame]:
    """yf.download with one retry — Yahoo throttles/hiccups regularly."""
    import time
    import yfinance as yf
    last_err = None
    for i in range(attempts):
        try:
            data = yf.download(tickers, progress=False, **kwargs)
            if data is not None and not data.empty:
                return data
            last_err = "empty response"
        except Exception as e:
            last_err = e
        if i < attempts - 1:
            time.sleep(3)
    logger.error(f"yfinance download failed after {attempts} attempts: {last_err}")
    return None


def _extract_single_ticker(data: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """
    Normalize a single-ticker yf.download result to flat OHLCV columns.

    Newer yfinance versions return MultiIndex columns even for one ticker
    (either (ticker, field) with group_by="ticker" or (field, ticker)).
    Blindly calling dropna(subset=["Close"]) on those raises KeyError —
    which matters because daily_check often fetches exactly one ticker.
    """
    try:
        df = data.copy()
        if isinstance(df.columns, pd.MultiIndex):
            lvl0 = set(df.columns.get_level_values(0))
            lvl1 = set(df.columns.get_level_values(1))
            if ticker in lvl0:
                df = df[ticker]
            elif ticker in lvl1:
                df = df.xs(ticker, axis=1, level=1)
            else:
                df.columns = df.columns.get_level_values(0)
        if "Close" not in df.columns:
            return None
        return df.dropna(subset=["Close"])
    except Exception as e:
        logger.warning(f"Could not normalize columns for {ticker}: {e}")
        return None


def last_price(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def avg_volume_20d(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty or "Volume" not in df.columns:
        return None
    if len(df) < 5:
        return None
    return float(df["Volume"].tail(20).mean())


def recent_realized_vol(df: pd.DataFrame, window: int = 20) -> Optional[float]:
    """Annualized realized vol from last `window` days of log returns."""
    import numpy as np
    if df is None or df.empty or len(df) < window + 5:
        return None
    close = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < window:
        return None
    sigma_daily = log_ret.tail(window).std()
    return float(sigma_daily * np.sqrt(252))


def get_vix_level() -> Optional[float]:
    """
    Current VIX close from yfinance.

    Handles the MultiIndex-columns format newer yfinance returns even for a
    single symbol — the old float(data["Close"].iloc[-1]) silently threw and
    left VIX as None, which meant the VIX kill switch could NEVER fire.
    """
    try:
        data = _download_with_retry("^VIX", period="5d")
        if data is None or data.empty:
            return None
        close = data["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if close.empty:
            return None
        return float(close.iloc[-1])
    except Exception as e:
        logger.warning(f"VIX fetch failed (kill switch inoperative this run): {e}")
        return None
