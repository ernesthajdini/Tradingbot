"""
StockTwits sentiment scraper.
StockTwits is a social platform specifically for traders — very useful signal.
No API key needed for basic access.
"""

import logging
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


class StockTwitsScraper:
    """Scrape StockTwits for trader sentiment."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })
        self._cache: dict[str, dict] = {}

    def get_ticker_sentiment(self, ticker: str) -> dict:
        """
        Get StockTwits sentiment for a ticker.
        Returns bullish/bearish ratio from actual trader posts.
        """
        cache_key = f"st_{ticker}_{datetime.now().strftime('%Y%m%d_%H')}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            time.sleep(1)
            url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
            resp = self.session.get(url, timeout=10)

            if resp.status_code != 200:
                return self._empty_result(ticker)

            data = resp.json()
            messages = data.get("messages", [])

            if not messages:
                return self._empty_result(ticker)

            bullish = 0
            bearish = 0
            total = 0

            recent_messages = []
            for msg in messages[:30]:
                total += 1
                sentiment = msg.get("entities", {}).get("sentiment", {})
                if sentiment:
                    if sentiment.get("basic") == "Bullish":
                        bullish += 1
                    elif sentiment.get("basic") == "Bearish":
                        bearish += 1

                recent_messages.append({
                    "body": msg.get("body", "")[:150],
                    "sentiment": sentiment.get("basic", "neutral") if sentiment else "neutral",
                    "likes": msg.get("likes", {}).get("total", 0),
                    "created": msg.get("created_at", ""),
                })

            # Calculate sentiment score
            labeled = bullish + bearish
            if labeled > 0:
                score = (bullish - bearish) / labeled
            else:
                score = 0.0

            # StockTwits also provides a watchlist count
            symbol_info = data.get("symbol", {})
            watchlist_count = symbol_info.get("watchlist_count", 0)

            result = {
                "ticker": ticker,
                "total_messages": total,
                "bullish_count": bullish,
                "bearish_count": bearish,
                "sentiment_score": round(score, 3),
                "sentiment_label": "bullish" if score > 0.2 else ("bearish" if score < -0.2 else "neutral"),
                "watchlist_count": watchlist_count,
                "recent_messages": recent_messages[:5],
                "source": "stocktwits",
                "scraped_at": datetime.now().isoformat(),
            }

            self._cache[cache_key] = result
            return result

        except Exception as e:
            logger.debug(f"StockTwits scrape failed for {ticker}: {e}")
            return self._empty_result(ticker)

    def get_trending(self) -> list[dict]:
        """Get trending tickers on StockTwits."""
        try:
            url = "https://api.stocktwits.com/api/2/trending/symbols.json"
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return []

            data = resp.json()
            symbols = data.get("symbols", [])

            return [{
                "ticker": s.get("symbol", ""),
                "title": s.get("title", ""),
                "watchlist_count": s.get("watchlist_count", 0),
            } for s in symbols[:20]]

        except Exception as e:
            logger.debug(f"StockTwits trending failed: {e}")
            return []

    def _empty_result(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "total_messages": 0,
            "bullish_count": 0,
            "bearish_count": 0,
            "sentiment_score": 0.0,
            "sentiment_label": "neutral",
            "watchlist_count": 0,
            "recent_messages": [],
            "source": "stocktwits",
        }
