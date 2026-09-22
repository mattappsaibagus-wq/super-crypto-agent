"""SentimentAgent — multi-source sentiment with weighted aggregation.

Fetches Reddit, Twitter, and NewsAPI, computes a composite sentiment per coin,
and emits `sentiment_shot` (bullish) or `sentiment_bear` (bearish) signals.
"""

from __future__ import annotations

import os
import time

from supercrypto.config import (
    NEWSAPI_BASE,
    REDDIT_API_BASE,
    SENTIMENT_MIN_SOURCES,
    SENTIMENT_REDDIT_LIMIT,
    SENTIMENT_WINDOW_DAYS,
)
from supercrypto.core.base import BaseAgent, api_get


SENTIMENT_COINS = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "MATIC"]
BULLISH_WORDS = {
    "moon", "apes", "diamond", "hodl", "bull", "rally", "surge", "pump",
    "gem", "buy", "long", "bullrun", "ath", "mvp", "legendary", "chad",
}
BEARISH_WORDS = {
    "bear", "dump", "crash", "rug", "scam", "shit", "fud", "scared",
    "sell", "short", "rekt", "loser", "garbage", "trash", "dead", "exit",
}
NEGATIONS = {"not", "no", "never", "without", "against", "failing", "fails"}


def _tokenize(text: str) -> list[str]:
    import re
    return re.findall(r"[a-z']+", text.lower())


def _score_text(text: str) -> float:
    tokens = _tokenize(text)
    score = 0.0
    for i, tok in enumerate(tokens):
        window = tokens[max(0, i - 3):i]
        negated = any(n in window for n in NEGATIONS)
        if tok in BULLISH_WORDS:
            score += -0.3 if negated else 0.3
        elif tok in BEARISH_WORDS:
            score += 0.3 if negated else -0.3
    return score


def _sentiment_from_items(items: list[str]) -> tuple[float, bool]:
    if not items:
        return 0.0, False
    total = sum(_score_text(t) for t in items)
    return total / len(items), True


class SentimentAgent(BaseAgent):
    NAME = "sentiment"
    EMOJI = "💭"

    def default_weights(self):
        return {
            "sentiment_shot": 0.55,
            "sentiment_bear": 0.55,
        }

    def run(self, **kwargs):
        coins = kwargs.get("coins") or SENTIMENT_COINS
        coins = [c.upper() for c in coins]
        signals = []

        for coin in coins:
            reddit_score, has_reddit = self._reddit_sentiment(coin)
            twitter_score, has_twitter = self._twitter_sentiment(coin)
            news_score, has_news = self._newsapi_sentiment(coin)

            scores = [s for s in [reddit_score, twitter_score, news_score] if s != 0.0]
            have = sum([has_reddit, has_twitter, has_news])
            if have < SENTIMENT_MIN_SOURCES or not scores:
                continue

            avg = sum(scores) / len(scores)
            abs_avg = abs(avg)
            if abs_avg < 0.15:
                continue

            if avg > 0:
                sig_name = "sentiment_shot"
            else:
                sig_name = "sentiment_bear"

            confidence = round(min(1.0, abs_avg * 2.0), 2)
            signals.append(
                {
                    "coin": coin,
                    "signal": sig_name,
                    "confidence": confidence,
                    "source": "sentiment_aggregated",
                    "details": {
                        "reasons": [
                            f"reddit={reddit_score:.2f} ({has_reddit})",
                            f"twitter={twitter_score:.2f} ({has_twitter})",
                            f"news={news_score:.2f} ({has_news})",
                            f"avg_sentiment={avg:.2f}",
                        ],
                        "reddit_score": round(reddit_score, 3),
                        "twitter_score": round(twitter_score, 3),
                        "news_score": round(news_score, 3),
                        "sources": have,
                    },
                }
            )
        return signals

    def _reddit_sentiment(self, coin: str) -> tuple[float, bool]:
        subreddits = {
            "BTC": "Bitcoin", "ETH": "ethereum", "BNB": "binance",
            "SOL": "solana", "DOGE": "dogecoin", "XRP": "ripple",
            "ADA": "cardano", "AVAX": "avalanche", "LINK": "Chainlink",
            "MATIC": "polygon",
        }
        sub = subreddits.get(coin)
        if not sub:
            return 0.0, False
        data = api_get(
            f"{REDDIT_API_BASE}/{sub}/new.json",
            params={"limit": SENTIMENT_REDDIT_LIMIT},
        )
        if not isinstance(data, dict):
            return 0.0, False
        posts = data.get("data", {}).get("children", [])
        texts = [(p.get("data", {}).get("title", "") + " " + p.get("data", {}).get("selftext", "")) for p in posts]
        return _sentiment_from_items(texts)

    def _twitter_sentiment(self, coin: str) -> tuple[float, bool]:
        data = api_get(
            f"https://api.twitterapi.xyz/v2/tweets/search/recent",
            params={"query": f"${coin} -is:retweet", "max_results": 100},
        )
        if not isinstance(data, dict):
            return 0.0, False
        tweets = data.get("data", [])
        texts = [t.get("text", "") for t in tweets]
        return _sentiment_from_items(texts)

    def _newsapi_sentiment(self, coin: str) -> tuple[float, bool]:
        api_key = os.environ.get("NEWSAPI_KEY")
        if not api_key:
            return 0.0, False
        data = api_get(
            NEWSAPI_BASE,
            params={
                "q": coin,
                "language": "en",
                "pageSize": 50,
                "from": f"{SENTIMENT_WINDOW_DAYS} days ago",
                "apiKey": api_key,
            },
        )
        if not isinstance(data, dict):
            return 0.0, False
        articles = data.get("articles", [])
        texts = [a.get("title", "") + " " + (a.get("description") or "") for a in articles]
        return _sentiment_from_items(texts)
