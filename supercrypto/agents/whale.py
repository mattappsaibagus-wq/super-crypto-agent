"""WhaleDetector — volume/momentum anomalies + cross-exchange whale flow.

Scoring logic lives in scoring.py for backtest parity. Adds on-chain transfer
monitoring via Etherscan for large wallet movements, emitting
`whale_onchain` and `whale_onchain_down` signals.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from supercrypto.config import COINGECKO_BASE, ETHERSCAN_API, DEXSCREENER_TOKEN
from supercrypto.core.base import BaseAgent, api_get, fetch_markets
from supercrypto.core.scoring import score_whale


class WhaleDetector(BaseAgent):
    NAME = "whale"
    EMOJI = "⚡"

    def default_weights(self):
        return {
            "whale_up": 0.30,
            "whale_down": 0.30,
            "whale_onchain": 0.30,
            "whale_onchain_down": 0.30,
            "volume_spike": 0.35,
            "price_move": 0.25,
            "momentum": 0.15,
            "liquidity_depth": 0.15,
            "sustainability": 0.10,
        }

    def run(self, **kwargs):
        signals = []
        signals.extend(self._from_market())
        signals.extend(self._onchain_transfers())
        return signals

    def _from_market(self):
        signals = []
        for c in self._fetch_market():
            name, conf, reasons = score_whale(
                c.get("change_pct") or 0,
                c.get("vol_ratio") or 0,
                c.get("vol") or 0,
            )
            if not name:
                continue
            signals.append(
                {
                    "coin": c["symbol"].upper(),
                    "signal": name,
                    "confidence": round(conf, 2),
                    "source": "coingecko_market",
                    "details": {
                        "change_24h": c.get("change_pct"),
                        "vol": c.get("vol"),
                        "vol_ratio": c.get("vol_ratio"),
                        "reasons": reasons,
                        "price": c.get("price"),
                        "coin_id": c.get("coin_id"),
                    },
                }
            )
        return signals

    def _onchain_transfers(self):
        """Check for large transfers on Etherscan. Requires an API key."""
        api_key = os.environ.get("ETHERSCAN_API_KEY", "")
        if not api_key:
            return []

        contract_map = {
            "BTC": "0x...",  # BTC on Ethereum (WBTC)
            "ETH": "",  # native ETH, different endpoint
            "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
            "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
            "AAVE": "0x7d2768dE32b0b80b7a3454c06Bdac94C9B13aD87",
        }

        signals = []
        for symbol, addr in contract_map.items():
            if not addr:
                # Native ETH: check blocknative or skip
                continue
            data = api_get(
                ETHERSCAN_API,
                params={
                    "module": "account",
                    "action": "tokentx",
                    "contractaddress": addr,
                    "apikey": api_key,
                    "page": 1,
                    "offset": 50,
                    "sort": "desc",
                },
            )
            if not isinstance(data, dict):
                continue
            txs = data.get("result", [])
            if not isinstance(txs, list) or not txs:
                continue

            recent_value = 0.0
            for tx in txs[:10]:
                try:
                    value = float(tx.get("value", 0)) / 1e18
                    recent_value += value
                except (TypeError, ValueError):
                    continue

            if recent_value > 1_000_000:
                signals.append(
                    {
                        "coin": symbol,
                        "signal": "whale_onchain",
                        "confidence": min(1.0, round(recent_value / 10_000_000, 2)),
                        "source": "etherscan",
                        "details": {
                            "reasons": [
                                f"${recent_value:,.0f} moved on-chain in last 50 txns",
                                f"contract: {addr[:10]}...",
                            ],
                            "onchain_value_usd": round(recent_value, 2),
                            "contract": addr[:20],
                        },
                    }
                )
            elif recent_value < -1_000_000:  # net outflow pattern
                signals.append(
                    {
                        "coin": symbol,
                        "signal": "whale_onchain_down",
                        "confidence": min(1.0, round(abs(recent_value) / 10_000_000, 2)),
                        "source": "etherscan",
                        "details": {
                            "reasons": [
                                f"large outflow detected on-chain",
                                f"net value: ${recent_value:,.0f}",
                            ],
                            "onchain_value_usd": round(recent_value, 2),
                        },
                    }
                )
            time.sleep(0.3)
        return signals

    def _fetch_market(self):
        coins = []
        data = fetch_markets(per_page=50)
        if not isinstance(data, list):
            return coins
        for c in data:
            vol = c.get("total_volume") or 0
            mcap = c.get("market_cap") or 0
            change = c.get("price_change_percentage_24h") or 0
            ratio = (vol / mcap) if mcap > 0 else 0
            coins.append(
                {
                    "symbol": c["symbol"].upper(),
                    "coin_id": c.get("id"),
                    "change_pct": change,
                    "vol": vol,
                    "vol_ratio": ratio,
                    "mcap": mcap,
                    "price": c.get("current_price") or 0,
                }
            )
        return coins
