"""MicroCapFinder — trending + rank-band + DEX new pairs. Discovery only."""

from __future__ import annotations

import time

from supercrypto.config import COINGECKO_BASE, DEXSCREENER_SEARCH
from supercrypto.core.base import BaseAgent, api_get


class MicroCapFinder(BaseAgent):
    NAME = "microcap"
    EMOJI = "🔎"

    def default_weights(self):
        return {
            "microcap_opportunity": 0.40,
            "strong_volume_ratio": 0.3,
            "low_liquidity_risk": 0.2,
            "age_signal": 0.15,
            "trending_boost": 0.2,
            "dex_listing": 0.15,
        }

    def run(self, **kwargs):
        found = self._scan_trending() + self._scan_dex()
        signals = []
        for coin in found[:20]:
            score, reasons = coin["score"], coin["reasons"]
            if score >= 0.5:
                signals.append(
                    {
                        "coin": coin["symbol"],
                        "signal": "microcap_opportunity",
                        "confidence": round(min(score, 1.0), 2),
                        "source": coin["source"],
                        "details": {
                            "mcap": coin.get("mcap"),
                            "vol": coin.get("vol24"),
                            "age_days": coin.get("age_days"),
                            "reasons": reasons,
                            "price": coin.get("price"),
                            "coin_id": coin.get("coin_id"),
                        },
                    }
                )
        return signals

    def _scan_trending(self):
        found = []
        payload = api_get(f"{COINGECKO_BASE}/search/trending")
        if isinstance(payload, dict):
            for item in payload.get("coins", [])[:20]:
                d = item.get("item", {})
                data = d.get("data", {}) or {}
                mcap = data.get("market_cap")
                price = data.get("price")
                score = 0.55
                reasons = ["trending on CoinGecko right now"]
                if isinstance(mcap, (int, float)) and mcap < 20_000_000:
                    score += 0.15
                    reasons.append("early mcap < $20M")
                found.append(
                    {
                        "symbol": d.get("symbol", "?").upper(),
                        "coin_id": d.get("id"),
                        "source": "coingecko_trending",
                        "score": min(score, 1.0),
                        "mcap": mcap,
                        "vol24": None,
                        "age_days": None,
                        "price": price,
                        "reasons": reasons,
                    }
                )

        coins = (
            api_get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": 4,
                    "sparkline": "false",
                },
            )
            or []
        )
        if not isinstance(coins, list):
            coins = []
        for c in coins:
            mcap = c.get("market_cap") or 0
            vol = c.get("total_volume") or 0
            price = c.get("current_price") or 0
            if not (mcap < 50_000_000 and vol > 50_000 and price > 0.00000001):
                continue
            vol_ratio = vol / mcap if mcap > 0 else 0
            score = 0.0
            reasons = []
            if vol_ratio > 0.3:
                score += 0.3
                reasons.append("volume/mcap {:.2f} (>0.3)".format(vol_ratio))
            if mcap < 10_000_000:
                score += 0.2
                reasons.append("very early mcap")
            change = c.get("price_change_percentage_24h") or 0
            if change > 15:
                score += 0.15
                reasons.append("24h momentum {:+.1f}%".format(change))
            rank = c.get("market_cap_rank")
            if rank and rank < 2000:
                score += 0.1
                reasons.append("ranked #{}".format(rank))
            found.append(
                {
                    "symbol": c["symbol"].upper(),
                    "coin_id": c.get("id"),
                    "source": "coingecko_microcap",
                    "score": min(score, 1.0),
                    "mcap": mcap,
                    "vol24": vol,
                    "age_days": None,
                    "price": price,
                    "reasons": reasons,
                }
            )
        return found

    def _scan_dex(self):
        found = []
        payload = api_get(DEXSCREENER_SEARCH, params={"q": "new"})
        pairs = (payload or {}).get("pairs", []) if isinstance(payload, dict) else []
        now_ms = int(time.time() * 1000)
        for p in pairs[:100]:
            liq = (p.get("liquidity") or {}).get("usd") or 0
            vol = (p.get("volume") or {}).get("h24") or 0
            created_at = p.get("pairCreatedAt")
            age_days = None
            if created_at:
                age_days = max(0, (now_ms - created_at) / 86400000)
            if liq > 50_000 and age_days is not None and age_days < 30:
                try:
                    price_usd = float(p.get("priceUsd") or 0)
                except (TypeError, ValueError):
                    price_usd = 0.0
                if price_usd < 0.001 and vol > 5_000:
                    score = 0.5
                    reasons = [
                        "listed {:.0f}d ago on {}".format(age_days, p.get("dexId", "dex"))
                    ]
                    if liq > 200_000:
                        score += 0.2
                        reasons.append("liquidity > $200k")
                    if p.get("fdv", 0) and p["fdv"] < 20_000_000:
                        score += 0.15
                        reasons.append("small FDV")
                    symbol = (p.get("baseToken") or {}).get("symbol", "?").upper()
                    found.append(
                        {
                            "symbol": symbol,
                            "source": "dexscreener",
                            "score": min(score, 1.0),
                            "mcap": p.get("fdv"),
                            "vol24": vol,
                            "age_days": round(age_days, 1),
                            "price": price_usd,
                            "reasons": reasons,
                        }
                    )
        return found
