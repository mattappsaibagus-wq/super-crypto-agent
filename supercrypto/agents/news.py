"""NewsScanner — multi-source news aggregation with credibility ranking.

Pulls from CoinPaprika, NewsAPI, and CryptoPanic. Uses bag-of-words
classification with negation detection and source credibility weighting.
"""

from __future__ import annotations

import hashlib
import os
import re

from supercrypto.config import (
    COINPAPRIKA_SEARCH,
    NEWSAPI_BASE,
)
from supercrypto.core.base import BaseAgent, api_get


WATCH_COINS = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "MATIC"]

SLUGS = {
    "BTC": "btc-bitcoin",
    "ETH": "eth-ethereum",
    "BNB": "bnb-binance-coin",
    "SOL": "sol-solana",
    "XRP": "xrp-xrp",
    "ADA": "ada-cardano",
    "DOGE": "doge-dogecoin",
    "AVAX": "avax-avalanche",
    "LINK": "link-chainlink",
    "MATIC": "matic-polygon",
}

BULLISH = [
    "surges", "rallies", "all-time high", "ath", "adopts", "etf approved",
    "partnership", "integrates", "wins", "record", "bullish", "buys",
    "accumulat", "breakout", "green lit", "cleared",
]
BEARISH = [
    "crash", "plummets", "dips", "hacked", "exploit", "sued", "banned",
    "crackdown", "dumps", "bearish", "delisted", "ponzi", "freeze",
    "insolvent", "rug", "liquidated", "hack",
]
NEGATIONS = {"not", "no", "never", "without", "fails", "failed", "unlikely", "despite", "down"}

SOURCE_WEIGHTS = {
    "coindesk.com": 0.85,
    "theblock.co": 0.80,
    "bloomberg.com": 0.90,
    "reuters.com": 0.90,
    "wsj.com": 0.85,
    "ft.com": 0.85,
    "cointelegraph.com": 0.55,
    "decrypt.co": 0.60,
    "thedefiant.io": 0.70,
    "dlnews.com": 0.70,
    "cryptopanic.com": 0.65,
}


def _host(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url or "")
    return (m.group(1).lower() if m else "") or ""


def _negated(text: str, phrase: str) -> bool:
    idx = text.find(phrase)
    if idx < 0:
        return False
    prefix = text[max(0, idx - 40):idx]
    tokens = re.findall(r"[a-z']+", prefix)
    return any(t in NEGATIONS for t in tokens[-4:])


def classify_headline(text: str, url: str = "") -> tuple[float, str, list[str]]:
    """Return (abs_score, signal_name, reasons). signal_name empty if nothing hit."""
    low = text.lower()
    score = 0.0
    reasons = []
    for w in BULLISH:
        if w in low:
            delta = -0.15 if _negated(low, w) else 0.15
            score += delta
            reasons.append("{}: '{}'".format("negated-bullish" if delta < 0 else "bullish", w))
    for w in BEARISH:
        if w in low:
            delta = 0.15 if _negated(low, w) else -0.15
            score += delta
            reasons.append("{}: '{}'".format("negated-bearish" if delta > 0 else "bearish", w))
    if not reasons:
        return 0.0, "", []

    host = _host(url)
    cred = SOURCE_WEIGHTS.get(host, 0.5)
    score *= cred
    score = max(-1.0, min(1.0, score))
    if abs(score) < 0.08:
        return 0.0, "", reasons

    name = "news_bullish" if score > 0 else "news_bearish"
    return abs(score), name, reasons + ["source_cred={:.2f} ({})".format(cred, host or "unknown")]


class NewsScanner(BaseAgent):
    NAME = "news"
    EMOJI = "📰"

    def default_weights(self):
        return {
            "news_event": 0.20,
            "news_bullish": 0.22,
            "news_bearish": 0.22,
        }

    def run(self, **kwargs):
        coins = kwargs.get("coins") or WATCH_COINS
        coins = [c.upper() for c in coins]
        signals = []

        # Source 1: CoinPaprika
        signals.extend(self._from_coinpaprika(coins))
        # Source 2: NewsAPI
        signals.extend(self._from_newsapi(coins))

        return signals

    def _from_coinpaprika(self, coins: list[str]) -> list:
        query = " ".join(SLUGS.get(c, c.lower()) for c in coins)
        payload = api_get(COINPAPRIKA_SEARCH, params={"q": query, "c": "events,news"})
        news = (payload or {}).get("news", []) if isinstance(payload, dict) else []
        return self._process_news_items(news, coins, "coinpaprika_news", source_field="link")

    def _from_newsapi(self, coins: list[str]) -> list:
        api_key = os.environ.get("NEWSAPI_KEY")
        if not api_key:
            return []
        query_parts = []
        for c in coins:
            q = SLUGS.get(c, c.lower())
            query_parts.append(q)
        query = " OR ".join(query_parts)
        data = api_get(
            NEWSAPI_BASE,
            params={
                "q": query,
                "language": "en",
                "pageSize": 50,
                "apiKey": api_key,
            },
        )
        articles = (data or {}).get("articles", []) if isinstance(data, dict) else []
        return self._process_news_items(articles, coins, "newsapi", source_field="url")

    def _process_news_items(self, items: list, coins: list[str], source_label: str, source_field: str) -> list:
        seen = set()
        signals = []
        for item in items[:50]:
            title = item.get("title") or item.get("title", "") or ""
            body = item.get("article") or item.get("description") or title
            url = item.get(source_field, "") or ""
            digest = hashlib.sha1((title + url).encode("utf-8")).hexdigest()[:12]
            if digest in seen:
                continue
            seen.add(digest)
            text = "{} {}".format(title, body)
            coin_hit = next((c for c in coins if c.lower() in text.lower()), None)
            if not coin_hit:
                continue
            strength, name, reasons = classify_headline(text, url)
            if not name:
                continue
            signals.append(
                {
                    "coin": coin_hit,
                    "signal": name,
                    "confidence": round(strength, 2),
                    "source": source_label,
                    "details": {
                        "headline": title,
                        "url": url,
                        "reasons": reasons,
                    },
                }
            )
            signals.append(
                {
                    "coin": coin_hit,
                    "signal": "news_event",
                    "confidence": round(strength, 2),
                    "source": source_label,
                    "details": {
                        "headline": title,
                        "url": url,
                        "reasons": reasons,
                        "direction": name,
                    },
                }
            )
        return signals
