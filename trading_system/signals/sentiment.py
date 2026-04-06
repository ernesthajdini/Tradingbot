"""
Sentiment analysis module.
Uses free news APIs to score market sentiment for tickers.
Supports:
  - Finnhub (free tier: 60 calls/min)
  - NewsAPI (free tier: 100 calls/day)
  - Yahoo Finance news (no API key needed, via yfinance)
"""

import logging
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)


class SentimentAnalyzer:
    """
    Fetches and scores news sentiment for stocks.
    Uses a simple keyword-based scoring when no ML model is available,
    and can integrate with external NLP APIs.
    """

    # Weighted keyword dictionaries for basic sentiment scoring
    POSITIVE_WORDS = {
        "beat": 2, "beats": 2, "exceeded": 2, "surpassed": 2, "record": 1.5,
        "upgrade": 2, "upgraded": 2, "buy": 1, "outperform": 1.5,
        "growth": 1, "profit": 1, "gains": 1, "rally": 1.5, "surge": 1.5,
        "strong": 1, "bullish": 2, "breakout": 1.5, "positive": 1,
        "innovation": 1, "launch": 0.5, "partnership": 0.5, "acquisition": 0.5,
        "dividend": 1, "buyback": 1, "raised": 1, "optimistic": 1.5,
        "recovery": 1, "momentum": 1, "expansion": 1, "approval": 1.5,
    }

    NEGATIVE_WORDS = {
        "miss": -2, "missed": -2, "below": -1, "decline": -1.5, "loss": -1.5,
        "downgrade": -2, "downgraded": -2, "sell": -1, "underperform": -1.5,
        "warning": -1.5, "layoff": -1.5, "layoffs": -1.5, "cut": -1,
        "weak": -1, "bearish": -2, "crash": -2, "negative": -1,
        "lawsuit": -1.5, "investigation": -1.5, "recall": -1.5, "fraud": -2,
        "debt": -1, "bankruptcy": -2.5, "default": -2, "risk": -0.5,
        "volatility": -0.5, "concern": -1, "uncertainty": -1, "recession": -1.5,
    }

    def __init__(self, finnhub_key: str | None = None, newsapi_key: str | None = None):
        self.finnhub_key = finnhub_key or os.environ.get("FINNHUB_API_KEY")
        self.newsapi_key = newsapi_key or os.environ.get("NEWSAPI_KEY")

    def get_sentiment(self, ticker: str, days_back: int = 7) -> dict:
        """
        Get sentiment score for a ticker from multiple sources.
        Returns dict with score (-1 to +1), article count, and details.
        """
        articles = []

        # Try each source
        if self.finnhub_key:
            articles.extend(self._fetch_finnhub(ticker, days_back))

        if self.newsapi_key:
            articles.extend(self._fetch_newsapi(ticker, days_back))

        # Always try yfinance (no key needed)
        articles.extend(self._fetch_yfinance_news(ticker))

        if not articles:
            return {
                "ticker": ticker,
                "score": 0.0,
                "label": "NEUTRAL",
                "confidence": 0.0,
                "article_count": 0,
                "source": "none",
                "details": [],
            }

        # Score each article
        scored = []
        for article in articles:
            score = self._score_text(article.get("title", "") + " " + article.get("summary", ""))
            scored.append({
                "title": article.get("title", "")[:100],
                "score": score,
                "source": article.get("source", "unknown"),
                "date": article.get("date", ""),
            })

        # Aggregate: recent articles weighted more
        scores = [s["score"] for s in scored]
        if scores:
            # Time-weighted average (recent = more weight)
            weights = np.linspace(0.5, 1.0, len(scores))
            avg_score = np.average(scores, weights=weights)
        else:
            avg_score = 0.0

        # Normalize to -1 to +1
        avg_score = np.clip(avg_score / 5.0, -1.0, 1.0)

        # Confidence based on article count and score consistency
        score_std = np.std(scores) if len(scores) > 1 else 1.0
        confidence = min(len(scores) / 10.0, 1.0) * max(0.2, 1.0 - score_std / 5.0)

        label = "BULLISH" if avg_score > 0.15 else "BEARISH" if avg_score < -0.15 else "NEUTRAL"

        return {
            "ticker": ticker,
            "score": round(float(avg_score), 3),
            "label": label,
            "confidence": round(float(confidence), 3),
            "article_count": len(scored),
            "source": "multi",
            "details": sorted(scored, key=lambda x: abs(x["score"]), reverse=True)[:5],
        }

    def get_batch_sentiment(self, tickers: list[str], days_back: int = 7) -> dict[str, dict]:
        """Get sentiment for multiple tickers."""
        results = {}
        for ticker in tickers:
            try:
                results[ticker] = self.get_sentiment(ticker, days_back)
            except Exception as e:
                logger.warning(f"Sentiment error for {ticker}: {e}")
                results[ticker] = {
                    "ticker": ticker, "score": 0.0, "label": "NEUTRAL",
                    "confidence": 0.0, "article_count": 0, "source": "error",
                }
        return results

    def _score_text(self, text: str) -> float:
        """Score a text string using keyword matching."""
        text_lower = text.lower()
        words = text_lower.split()
        score = 0.0

        for word in words:
            # Strip punctuation
            clean = word.strip(".,!?;:'\"()-")
            if clean in self.POSITIVE_WORDS:
                score += self.POSITIVE_WORDS[clean]
            elif clean in self.NEGATIVE_WORDS:
                score += self.NEGATIVE_WORDS[clean]

        return score

    def _fetch_finnhub(self, ticker: str, days_back: int) -> list[dict]:
        """Fetch news from Finnhub API."""
        try:
            end = datetime.now()
            start = end - timedelta(days=days_back)
            url = "https://finnhub.io/api/v1/company-news"
            params = {
                "symbol": ticker,
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
                "token": self.finnhub_key,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return [{
                    "title": a.get("headline", ""),
                    "summary": a.get("summary", ""),
                    "source": "finnhub",
                    "date": datetime.fromtimestamp(a.get("datetime", 0)).strftime("%Y-%m-%d"),
                } for a in data[:20]]
        except Exception as e:
            logger.debug(f"Finnhub fetch failed for {ticker}: {e}")
        return []

    def _fetch_newsapi(self, ticker: str, days_back: int) -> list[dict]:
        """Fetch news from NewsAPI."""
        try:
            end = datetime.now()
            start = end - timedelta(days=days_back)
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": f"{ticker} stock",
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 20,
                "apiKey": self.newsapi_key,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return [{
                    "title": a.get("title", ""),
                    "summary": a.get("description", ""),
                    "source": "newsapi",
                    "date": a.get("publishedAt", "")[:10],
                } for a in data.get("articles", [])[:20]]
        except Exception as e:
            logger.debug(f"NewsAPI fetch failed for {ticker}: {e}")
        return []

    def _fetch_yfinance_news(self, ticker: str) -> list[dict]:
        """Fetch news via yfinance (no API key needed)."""
        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            news = stock.news or []
            results = []
            for item in news[:15]:
                # yfinance >=0.2.36 nests content under 'content' key
                content = item.get("content", item) if isinstance(item, dict) else item
                title = content.get("title", "")
                summary = content.get("summary", title)
                pub_date = content.get("pubDate", "")
                if pub_date and len(pub_date) >= 10:
                    date_str = pub_date[:10]
                else:
                    date_str = ""
                results.append({
                    "title": title,
                    "summary": summary,
                    "source": "yahoo",
                    "date": date_str,
                })
            return results
        except Exception as e:
            logger.debug(f"yfinance news failed for {ticker}: {e}")
        return []
