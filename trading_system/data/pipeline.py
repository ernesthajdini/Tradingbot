"""
Data ingestion and cleaning pipeline.
Uses yfinance for price data (free, no API key needed).
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from trading_system.config.settings import DataConfig

logger = logging.getLogger(__name__)


class DataPipeline:
    """Handles data ingestion, cleaning, and caching."""

    def __init__(self, config: DataConfig | None = None):
        self.config = config or DataConfig()
        self.cache_dir = Path(self.config.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._price_cache: dict[str, pd.DataFrame] = {}

    def fetch_prices(
        self,
        tickers: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, pd.DataFrame]:
        """
        Fetch OHLCV data for a list of tickers.
        Returns dict of ticker -> DataFrame with columns:
        [Open, High, Low, Close, Adj Close, Volume]
        """
        tickers = tickers or self.config.universe
        if start is None:
            start = (datetime.now() - timedelta(days=365 * self.config.lookback_years)).strftime("%Y-%m-%d")
        if end is None:
            end = datetime.now().strftime("%Y-%m-%d")

        results = {}
        to_fetch = []

        for ticker in tickers:
            if use_cache and ticker in self._price_cache:
                results[ticker] = self._price_cache[ticker]
                continue

            cache_file = self.cache_dir / f"{ticker}_daily.parquet"
            if use_cache and cache_file.exists():
                df = pd.read_parquet(cache_file)
                last_date = df.index.max()
                if last_date.strftime("%Y-%m-%d") >= (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"):
                    results[ticker] = df
                    self._price_cache[ticker] = df
                    continue

            to_fetch.append(ticker)

        if to_fetch:
            logger.info(f"Fetching price data for {len(to_fetch)} tickers...")
            try:
                raw = yf.download(
                    to_fetch,
                    start=start,
                    end=end,
                    auto_adjust=False,
                    group_by="ticker",
                    threads=True,
                    progress=False,
                )

                for ticker in to_fetch:
                    try:
                        # yfinance returns MultiIndex columns: (Price, Ticker)
                        if isinstance(raw.columns, pd.MultiIndex):
                            # Drop the ticker level, keep just price columns
                            if len(to_fetch) == 1:
                                df = raw.droplevel("Ticker", axis=1).copy()
                            else:
                                df = raw.xs(ticker, level="Ticker", axis=1).copy()
                        elif len(to_fetch) == 1:
                            df = raw.copy()
                        else:
                            df = raw[ticker].copy()

                        df = self._clean_prices(df, ticker)
                        if df is not None and len(df) > 0:
                            cache_file = self.cache_dir / f"{ticker}_daily.parquet"
                            df.to_parquet(cache_file)
                            results[ticker] = df
                            self._price_cache[ticker] = df
                            logger.info(f"  {ticker}: {len(df)} days loaded")
                        else:
                            logger.warning(f"  {ticker}: no valid data")
                    except Exception as e:
                        logger.warning(f"  {ticker}: failed - {e}")

            except Exception as e:
                logger.error(f"Batch download failed: {e}")

        logger.info(f"Loaded data for {len(results)}/{len(tickers)} tickers")
        return results

    def _clean_prices(self, df: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
        """Clean and validate price data."""
        if df is None or df.empty:
            return None

        # Flatten multi-level columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        # Ensure required columns exist
        required = ["Open", "High", "Low", "Close", "Volume"]
        for col in required:
            if col not in df.columns:
                logger.warning(f"{ticker}: missing column {col}")
                return None

        # Drop rows where all price columns are NaN
        df = df.dropna(subset=["Close"])

        # Forward-fill small gaps (up to 3 days, e.g., holidays)
        df = df.asfreq("B")  # business day frequency
        df[["Open", "High", "Low", "Close"]] = df[["Open", "High", "Low", "Close"]].ffill(limit=3)
        df["Volume"] = df["Volume"].fillna(0)

        # Drop remaining NaN rows
        df = df.dropna(subset=["Close"])

        # Flag extreme moves (>20% daily) but keep them
        returns = df["Close"].pct_change()
        extreme = returns.abs() > 0.20
        if extreme.any():
            dates = df.index[extreme].strftime("%Y-%m-%d").tolist()
            logger.info(f"  {ticker}: extreme moves on {dates}")

        # Use Adj Close for adjusted prices, fall back to Close
        if "Adj Close" in df.columns:
            df["Adj Close"] = df["Adj Close"].fillna(df["Close"])
        else:
            df["Adj Close"] = df["Close"]

        return df

    def fetch_benchmark(self, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """Fetch benchmark (SPY) data."""
        result = self.fetch_prices([self.config.benchmark], start=start, end=end)
        return result.get(self.config.benchmark, pd.DataFrame())

    def get_market_data(self, ticker: str) -> pd.DataFrame | None:
        """Get cached data for a single ticker, fetching if needed."""
        if ticker in self._price_cache:
            return self._price_cache[ticker]
        result = self.fetch_prices([ticker])
        return result.get(ticker)

    def clear_cache(self):
        """Clear all cached data."""
        self._price_cache.clear()
        for f in self.cache_dir.glob("*.parquet"):
            f.unlink()
        logger.info("Cache cleared")
