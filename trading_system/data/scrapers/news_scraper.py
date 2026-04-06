"""
Financial news scraper.
Pulls headlines from multiple free sources — no API keys needed.
Sources: Yahoo Finance, Finviz, MarketWatch RSS.
"""

import logging
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Sentiment word lists tuned for financial news
POSITIVE_WORDS = {
    "surge", "soar", "rally", "jump", "gain", "rise", "beat", "upgrade",
    "outperform", "bullish", "growth", "strong", "profit", "record",
    "breakthrough", "innovative", "exceeds", "positive", "optimistic",
    "upside", "momentum", "recovery", "boost", "expand", "dividend",
    "buyback", "acquisition", "partnership", "approval", "launch",
}

NEGATIVE_WORDS = {
    "crash", "plunge", "drop", "fall", "decline", "miss", "downgrade",
    "underperform", "bearish", "loss", "weak", "warning", "risk",
    "lawsuit", "fraud", "investigation", "recall", "layoff", "cut",
    "bankruptcy", "default", "downside", "concern", "fear", "sell-off",
    "recession", "inflation", "debt", "regulatory", "fine", "penalty",
}


class NewsScraper:
    """Scrape financial news from multiple free sources."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._cache: dict[str, dict] = {}

    def get_ticker_news(self, ticker: str, max_articles: int = 20) -> dict:
        """
        Get news and sentiment for a ticker from multiple sources.
        """
        cache_key = f"news_{ticker}_{datetime.now().strftime('%Y%m%d_%H')}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        all_articles = []

        # Source 1: Yahoo Finance
        yahoo_articles = self._scrape_yahoo_finance(ticker)
        all_articles.extend(yahoo_articles)

        # Source 2: Finviz
        finviz_articles = self._scrape_finviz(ticker)
        all_articles.extend(finviz_articles)

        # Deduplicate by title similarity
        seen_titles = set()
        unique_articles = []
        for article in all_articles:
            title_key = article["title"][:50].lower()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                unique_articles.append(article)

        # Score sentiment
        bullish = 0
        bearish = 0
        for article in unique_articles:
            title_lower = article["title"].lower()
            bull = sum(1 for w in POSITIVE_WORDS if w in title_lower)
            bear = sum(1 for w in NEGATIVE_WORDS if w in title_lower)

            if bull > bear:
                article["sentiment"] = "positive"
                bullish += 1
            elif bear > bull:
                article["sentiment"] = "negative"
                bearish += 1
            else:
                article["sentiment"] = "neutral"

        total = len(unique_articles)
        score = (bullish - bearish) / total if total > 0 else 0

        result = {
            "ticker": ticker,
            "total_articles": total,
            "sentiment_score": round(score, 3),
            "sentiment_label": "positive" if score > 0.15 else ("negative" if score < -0.15 else "neutral"),
            "positive_count": bullish,
            "negative_count": bearish,
            "neutral_count": total - bullish - bearish,
            "articles": unique_articles[:max_articles],
            "source": "news_multi",
            "scraped_at": datetime.now().isoformat(),
        }

        self._cache[cache_key] = result
        return result

    def _scrape_yahoo_finance(self, ticker: str) -> list[dict]:
        """Scrape Yahoo Finance news for a ticker."""
        try:
            url = f"https://finance.yahoo.com/quote/{ticker}/news/"
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            articles = []

            # Find news article links
            for link in soup.find_all("a", href=True):
                title_elem = link.find("h3") or link.find("h4")
                if not title_elem:
                    continue
                title = title_elem.get_text(strip=True)
                if len(title) < 15:
                    continue

                articles.append({
                    "title": title[:200],
                    "source": "Yahoo Finance",
                    "url": link["href"] if link["href"].startswith("http") else f"https://finance.yahoo.com{link['href']}",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                })

            return articles[:10]
        except Exception as e:
            logger.debug(f"Yahoo Finance scrape failed for {ticker}: {e}")
            return []

    def _scrape_finviz(self, ticker: str) -> list[dict]:
        """Scrape Finviz news table for a ticker."""
        try:
            time.sleep(1)  # rate limit
            url = f"https://finviz.com/quote.ashx?t={ticker}"
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            articles = []

            # Finviz news table
            news_table = soup.find("table", {"id": "news-table"})
            if not news_table:
                return []

            current_date = datetime.now().strftime("%Y-%m-%d")
            for row in news_table.find_all("tr")[:15]:
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                # First cell is date/time, second is the link
                date_cell = cells[0].get_text(strip=True)
                link = cells[1].find("a")
                if not link:
                    continue

                title = link.get_text(strip=True)
                href = link.get("href", "")

                # Parse date
                if re.match(r'\w{3}-\d{2}-\d{2}', date_cell):
                    current_date = datetime.strptime(date_cell[:9], "%b-%d-%y").strftime("%Y-%m-%d")

                articles.append({
                    "title": title[:200],
                    "source": "Finviz",
                    "url": href,
                    "date": current_date,
                })

            return articles
        except Exception as e:
            logger.debug(f"Finviz scrape failed for {ticker}: {e}")
            return []

    def get_market_headlines(self) -> list[dict]:
        """Get general market news headlines."""
        try:
            url = "https://finviz.com/news.ashx"
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            articles = []

            for link in soup.find_all("a", class_="nn-tab-link"):
                title = link.get_text(strip=True)
                if len(title) > 15:
                    articles.append({
                        "title": title[:200],
                        "source": "Finviz Market News",
                        "url": link.get("href", ""),
                    })

            return articles[:20]
        except Exception as e:
            logger.debug(f"Market headlines scrape failed: {e}")
            return []
