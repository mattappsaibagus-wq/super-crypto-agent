"""DueDiligence — all five DD weights are now actually computed.

ox-alpha defined holder_concentration and market_traction as weights but
`_score()` never produced a value for either. Both now feed score_dd():
- holder_concentration from DEXScreener pair-liquidity spread
- market_traction from 7d/30d change + rank
"""

from __future__ import annotations

from supercrypto.config import COINGECKO_BASE, DEXSCREENER_SEARCH
from supercrypto.core.base import BaseAgent, api_get, coin_id_for
from supercrypto.core.scoring import score_dd


class DueDiligence(BaseAgent):
    NAME = "dd"
    EMOJI = "🛡️"

    def default_weights(self):
        return {
            "dd_result": 0.50,
            "liquidity_health": 0.25,
            "holder_concentration": 0.25,
            "volume_depth": 0.2,
            "age_survivorship": 0.15,
            "market_traction": 0.15,
        }

    def run(self, **kwargs):
        candidates = self.read_signals()
        coin_map = {}
        for sig in candidates:
            c = sig.get("coin")
            if not c or c.upper() == "BTC" and sig.get("agent") == "macro":
                # Macro emits a BTC regime signal; don't DD-scan BTC just for that
                # unless another agent also flagged it.
                if sig.get("agent") == "macro":
                    continue
            if not c:
                continue
            entry = coin_map.setdefault(c, {})
            details = sig.get("details") or {}
            entry["id"] = entry.get("id") or details.get("coin_id")
            entry["conf"] = max(entry.get("conf", 0), sig.get("confidence", 0))

        ordered = sorted(coin_map.items(), key=lambda kv: kv[1].get("conf", 0), reverse=True)
        results = []
        for symbol, meta in ordered[:8]:
            info = self._fetch_coin_info(symbol, meta.get("id"))
            dex = self._dex_liquidity(symbol)
            info.update(dex)
            score, reasons = score_dd(info)
            results.append(
                {
                    "coin": symbol,
                    "signal": "dd_result",
                    "confidence": round(score, 2),
                    "source": "duediligence",
                    "details": {
                        "reasons": reasons,
                        "score_breakdown": info,
                        "price": info.get("price"),
                    },
                }
            )
        return results

    def _fetch_coin_info(self, symbol: str, coin_id: str = None) -> dict:
        info = {
            "mcap": 0,
            "vol": 0,
            "ath_change_pct": 0,
            "price": 0,
            "rank": None,
            "change_7d": None,
            "change_30d": None,
            "pair_count": 0,
            "top_pair_liq_share": 1.0,
        }
        cid = coin_id or coin_id_for(symbol)
        if not cid:
            info["rag_status"] = "unknown_to_major_indexers"
            return info
        payload = api_get(
            f"{COINGECKO_BASE}/coins/{cid}",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "false",
                "developer_data": "false",
            },
        )
        if not isinstance(payload, dict) or "market_data" not in payload:
            info["rag_status"] = "indexed_unverified"
            return info
        d = payload["market_data"]
        info.update(
            {
                "mcap": d.get("market_cap", {}).get("usd") or 0,
                "vol": d.get("total_volume", {}).get("usd") or 0,
                "ath_change_pct": d.get("ath_change_percentage", {}).get("usd") or 0,
                "price": d.get("current_price", {}).get("usd") or 0,
                "rank": d.get("market_cap_rank"),
                "change_7d": d.get("price_change_percentage_7d"),
                "change_30d": d.get("price_change_percentage_30d"),
                "rag_status": "indexed",
            }
        )
        return info

    def _dex_liquidity(self, symbol: str) -> dict:
        """Pair count + top-pair liquidity share. Free proxy for concentration."""
        payload = api_get(DEXSCREENER_SEARCH, params={"q": symbol})
        pairs = (payload or {}).get("pairs", []) if isinstance(payload, dict) else []
        # Keep pairs whose base token matches the ticker.
        matched = []
        for p in pairs[:25]:
            base = ((p.get("baseToken") or {}).get("symbol") or "").upper()
            if base == symbol.upper():
                liq = (p.get("liquidity") or {}).get("usd") or 0
                matched.append(liq)
        if not matched:
            return {"pair_count": 0, "top_pair_liq_share": 1.0}
        total = sum(matched) or 1.0
        top = max(matched)
        return {
            "pair_count": len(matched),
            "top_pair_liq_share": round(top / total, 3),
        }
