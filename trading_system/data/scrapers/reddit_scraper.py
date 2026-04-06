"""
Reddit sentiment scraper.
Pulls posts and comments from stock-related subreddits.
No API key required — uses Reddit's public JSON endpoints.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "stockmarket",
    "options",
]

# Simple sentiment word lists
BULLISH_WORDS = {
    "buy", "calls", "moon", "rocket", "bullish", "long", "undervalued",
    "breakout", "squeeze", "tendies", "yolo", "diamond hands", "hold",
    "green", "pump", "rally", "surge", "soar", "upside", "growth",
    "beat", "earnings beat", "upgrade", "strong buy", "accumulate",
}

BEARISH_WORDS = {
    "sell", "puts", "crash", "bear", "short", "overvalued", "dump",
    "red", "tank", "plunge", "drop", "fall", "downside", "decline",
    "miss", "earnings miss", "downgrade", "avoid", "bubble", "fraud",
    "bagholding", "rip", "dead", "worthless",
}

HEADERS = {
    "User-Agent": "TradingBot/1.0 (educational project)"
}


class RedditScraper:
    """Scrape Reddit for stock sentiment. No API key needed."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._cache: dict[str, dict] = {}
        self._last_request = 0

    def _rate_limit(self):
        """Respect Reddit's rate limits — 1 request per 2 seconds."""
        elapsed = time.time() - self._last_request
        if elapsed < 2:
            time.sleep(2 - elapsed)
        self._last_request = time.time()

    def get_ticker_mentions(
        self,
        ticker: str,
        subreddits: list[str] | None = None,
        limit: int = 25,
    ) -> dict:
        """
        Search Reddit for mentions of a ticker.
        Returns sentiment analysis of the posts found.
        """
        cache_key = f"{ticker}_{datetime.now().strftime('%Y%m%d_%H')}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        subreddits = subreddits or SUBREDDITS
        all_posts = []

        for sub in subreddits:
            try:
                posts = self._search_subreddit(sub, ticker, limit=limit)
                all_posts.extend(posts)
            except Exception as e:
                logger.debug(f"Reddit search failed for r/{sub}: {e}")

        if not all_posts:
            result = {
                "ticker": ticker,
                "total_mentions": 0,
                "sentiment_score": 0.0,
                "sentiment_label": "neutral",
                "bullish_count": 0,
                "bearish_count": 0,
                "neutral_count": 0,
                "top_posts": [],
                "source": "reddit",
            }
            self._cache[cache_key] = result
            return result

        # Analyze sentiment
        bullish = 0
        bearish = 0
        neutral = 0
        scored_posts = []

        for post in all_posts:
            text = f"{post.get('title', '')} {post.get('selftext', '')}".lower()
            bull_hits = sum(1 for w in BULLISH_WORDS if w in text)
            bear_hits = sum(1 for w in BEARISH_WORDS if w in text)

            if bull_hits > bear_hits:
                bullish += 1
                post["sentiment"] = "bullish"
            elif bear_hits > bull_hits:
                bearish += 1
                post["sentiment"] = "bearish"
            else:
                neutral += 1
                post["sentiment"] = "neutral"

            post["bull_hits"] = bull_hits
            post["bear_hits"] = bear_hits
            scored_posts.append(post)

        total = bullish + bearish + neutral
        # Score from -1 (very bearish) to +1 (very bullish)
        sentiment_score = (bullish - bearish) / total if total > 0 else 0

        if sentiment_score > 0.2:
            label = "bullish"
        elif sentiment_score < -0.2:
            label = "bearish"
        else:
            label = "neutral"

        # Sort by engagement
        scored_posts.sort(key=lambda p: p.get("score", 0), reverse=True)

        result = {
            "ticker": ticker,
            "total_mentions": total,
            "sentiment_score": round(sentiment_score, 3),
            "sentiment_label": label,
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "top_posts": [{
                "title": p.get("title", "")[:100],
                "subreddit": p.get("subreddit", ""),
                "score": p.get("score", 0),
                "comments": p.get("num_comments", 0),
                "sentiment": p.get("sentiment", "neutral"),
                "url": p.get("url", ""),
            } for p in scored_posts[:5]],
            "source": "reddit",
            "scraped_at": datetime.now().isoformat(),
        }

        self._cache[cache_key] = result
        return result

    def _search_subreddit(self, subreddit: str, query: str, limit: int = 25) -> list[dict]:
        """Search a subreddit using Reddit's JSON API."""
        self._rate_limit()

        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {
            "q": query,
            "sort": "relevance",
            "t": "week",  # last week
            "limit": min(limit, 25),
            "restrict_sr": "true",
        }

        try:
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code == 429:
                logger.warning("Reddit rate limited, waiting...")
                time.sleep(10)
                return []
            if resp.status_code != 200:
                return []

            data = resp.json()
            posts = []
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                posts.append({
                    "title": post.get("title", ""),
                    "selftext": post.get("selftext", "")[:500],
                    "score": post.get("score", 0),
                    "num_comments": post.get("num_comments", 0),
                    "subreddit": subreddit,
                    "created": post.get("created_utc", 0),
                    "url": f"https://reddit.com{post.get('permalink', '')}",
                })
            return posts
        except Exception as e:
            logger.debug(f"Reddit request failed: {e}")
            return []

    def get_trending_tickers(self, subreddits: list[str] | None = None) -> list[dict]:
        """
        Find which tickers are being mentioned most on Reddit right now.
        Useful for discovering momentum plays and retail interest.
        """
        subreddits = subreddits or ["wallstreetbets", "stocks"]
        ticker_counts: dict[str, int] = {}
        ticker_sentiment: dict[str, list] = {}

        # Common ticker pattern: $AAPL or standalone uppercase 2-5 letter words
        ticker_pattern = re.compile(r'\$([A-Z]{2,5})\b|(?<!\w)([A-Z]{2,5})(?!\w)')

        # Exclude common English words that look like tickers
        false_tickers = {
            "THE", "FOR", "AND", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN",
            "HAD", "HER", "WAS", "ONE", "OUR", "OUT", "HAS", "HIS", "HOW",
            "ITS", "MAY", "NEW", "NOW", "OLD", "SEE", "WAY", "WHO", "DID",
            "GET", "HIM", "LET", "SAY", "SHE", "TOO", "USE", "CEO", "IPO",
            "ETF", "ATH", "DD", "IMO", "YOLO", "FOMO", "FD", "OTM", "ITM",
            "DTE", "IV", "WSB", "EDIT", "TLDR", "EPS", "PE", "GDP", "CPI",
            "FED", "SEC", "FDA", "AI", "EV", "USA", "USD", "EU", "UK",
        }

        for sub in subreddits:
            try:
                self._rate_limit()
                url = f"https://www.reddit.com/r/{sub}/hot.json"
                resp = self.session.get(url, params={"limit": 50}, timeout=10)
                if resp.status_code != 200:
                    continue

                data = resp.json()
                for child in data.get("data", {}).get("children", []):
                    post = child.get("data", {})
                    text = f"{post.get('title', '')} {post.get('selftext', '')}"

                    matches = ticker_pattern.findall(text)
                    for match in matches:
                        ticker = match[0] or match[1]
                        if ticker in false_tickers or len(ticker) < 2:
                            continue
                        ticker_counts[ticker] = ticker_counts.get(ticker, 0) + 1

                        # Quick sentiment
                        text_lower = text.lower()
                        bull = sum(1 for w in BULLISH_WORDS if w in text_lower)
                        bear = sum(1 for w in BEARISH_WORDS if w in text_lower)
                        if ticker not in ticker_sentiment:
                            ticker_sentiment[ticker] = []
                        ticker_sentiment[ticker].append(bull - bear)

            except Exception as e:
                logger.debug(f"Trending scrape failed for r/{sub}: {e}")

        # Sort by mention count
        trending = []
        for ticker, count in sorted(ticker_counts.items(), key=lambda x: x[1], reverse=True)[:20]:
            sentiments = ticker_sentiment.get(ticker, [0])
            avg_sent = sum(sentiments) / len(sentiments) if sentiments else 0
            trending.append({
                "ticker": ticker,
                "mentions": count,
                "sentiment_score": round(avg_sent, 2),
                "sentiment_label": "bullish" if avg_sent > 0.5 else ("bearish" if avg_sent < -0.5 else "neutral"),
            })

        return trending
