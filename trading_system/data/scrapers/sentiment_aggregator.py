"""
Sentiment Aggregator — combines signals from all data sources.
This is what gets fed into the signal generator as the "sentiment score".
"""

import logging
from datetime import datetime

from .reddit_scraper import RedditScraper
from .news_scraper import NewsScraper
from .stocktwits_scraper import StockTwitsScraper

logger = logging.getLogger(__name__)


class SentimentAggregator:
    """
    Combines sentiment from Reddit, news, and StockTwits
    into a single weighted score per ticker.
    """

    # How much to trust each source
    WEIGHTS = {
        "news": 0.40,        # Financial news — most reliable
        "stocktwits": 0.30,  # Trader-specific sentiment
        "reddit": 0.30,      # Retail/social sentiment
    }

    def __init__(self):
        self.reddit = RedditScraper()
        self.news = NewsScraper()
        self.stocktwits = StockTwitsScraper()

    def get_sentiment(self, ticker: str) -> dict:
        """
        Get aggregated sentiment for a single ticker.
        Returns combined score and breakdown by source.
        """
        results = {}
        scores = {}

        # Reddit
        try:
            reddit_data = self.reddit.get_ticker_mentions(ticker)
            results["reddit"] = reddit_data
            scores["reddit"] = reddit_data.get("sentiment_score", 0)
        except Exception as e:
            logger.debug(f"Reddit failed for {ticker}: {e}")
            scores["reddit"] = 0

        # News
        try:
            news_data = self.news.get_ticker_news(ticker)
            results["news"] = news_data
            scores["news"] = news_data.get("sentiment_score", 0)
        except Exception as e:
            logger.debug(f"News failed for {ticker}: {e}")
            scores["news"] = 0

        # StockTwits
        try:
            st_data = self.stocktwits.get_ticker_sentiment(ticker)
            results["stocktwits"] = st_data
            scores["stocktwits"] = st_data.get("sentiment_score", 0)
        except Exception as e:
            logger.debug(f"StockTwits failed for {ticker}: {e}")
            scores["stocktwits"] = 0

        # Weighted average
        combined_score = sum(
            scores.get(source, 0) * weight
            for source, weight in self.WEIGHTS.items()
        )

        # Label
        if combined_score > 0.15:
            label = "bullish"
        elif combined_score < -0.15:
            label = "bearish"
        else:
            label = "neutral"

        # Mention volume (signals retail interest)
        total_mentions = (
            results.get("reddit", {}).get("total_mentions", 0) +
            results.get("stocktwits", {}).get("total_messages", 0) +
            results.get("news", {}).get("total_articles", 0)
        )

        # High volume + strong sentiment = stronger signal
        volume_multiplier = min(1.0 + (total_mentions / 50) * 0.2, 1.5)
        adjusted_score = combined_score * volume_multiplier

        return {
            "ticker": ticker,
            "combined_score": round(adjusted_score, 3),
            "raw_score": round(combined_score, 3),
            "label": label,
            "total_mentions": total_mentions,
            "volume_multiplier": round(volume_multiplier, 2),
            "breakdown": {
                source: {
                    "score": round(scores.get(source, 0), 3),
                    "weight": weight,
                    "contribution": round(scores.get(source, 0) * weight, 3),
                }
                for source, weight in self.WEIGHTS.items()
            },
            "details": results,
            "scraped_at": datetime.now().isoformat(),
        }

    def get_batch_sentiment(self, tickers: list[str]) -> dict[str, dict]:
        """Get sentiment for multiple tickers."""
        results = {}
        for ticker in tickers:
            try:
                results[ticker] = self.get_sentiment(ticker)
            except Exception as e:
                logger.warning(f"Sentiment failed for {ticker}: {e}")
                results[ticker] = {
                    "ticker": ticker,
                    "combined_score": 0,
                    "label": "neutral",
                    "total_mentions": 0,
                }
        return results

    def get_market_mood(self) -> dict:
        """
        Get overall market mood from trending data.
        Useful for regime detection.
        """
        reddit_trending = []
        st_trending = []

        try:
            reddit_trending = self.reddit.get_trending_tickers()
        except Exception:
            pass

        try:
            st_trending = self.stocktwits.get_trending()
        except Exception:
            pass

        try:
            headlines = self.news.get_market_headlines()
        except Exception:
            headlines = []

        # Score headlines
        from .news_scraper import POSITIVE_WORDS, NEGATIVE_WORDS
        bull_headlines = 0
        bear_headlines = 0
        for h in headlines:
            title = h.get("title", "").lower()
            bull = sum(1 for w in POSITIVE_WORDS if w in title)
            bear = sum(1 for w in NEGATIVE_WORDS if w in title)
            if bull > bear:
                bull_headlines += 1
            elif bear > bull:
                bear_headlines += 1

        total_headlines = bull_headlines + bear_headlines
        news_mood = (bull_headlines - bear_headlines) / total_headlines if total_headlines > 0 else 0

        return {
            "news_mood": round(news_mood, 3),
            "news_label": "bullish" if news_mood > 0.15 else ("bearish" if news_mood < -0.15 else "neutral"),
            "reddit_trending": reddit_trending[:10],
            "stocktwits_trending": st_trending[:10],
            "headline_count": len(headlines),
            "scraped_at": datetime.now().isoformat(),
        }
