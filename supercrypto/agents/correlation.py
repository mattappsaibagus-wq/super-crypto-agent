"""CorrelationAgent — monitors cross-asset correlation spikes/dumps.

When BTC and ETH correlation suddenly spikes above threshold, it suggests
alt-season is starting or ending. Monitors ETH/BTC, ALT/BTC, and a basket
of major alts vs BTC.
"""

from __future__ import annotations

import time
from typing import Optional

from supercrypto.config import (
    COINGECKO_BASE,
    CORRELATION_DUMP_THRESHOLD,
    CORRELATION_SPIKE_THRESHOLD,
    CORRELATION_WINDOW,
)
from supercrypto.core.base import BaseAgent, api_get


CORRELATION_PAIRS = [
    ("ETH", "BTC"),
    ("SOL", "BTC"),
    ("ADA", "BTC"),
    ("AVAX", "BTC"),
    ("LINK", "BTC"),
    ("XRP", "BTC"),
]


def _pearson(x: list[float], y: list[float]) -> Optional[float]:
    n = min(len(x), len(y))
    if n < 5:
        return None
    x = x[-n:]
    y = y[-n:]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def _price_history(coin_id: str, days: int) -> list[float]:
    data = api_get(
        f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"},
    )
    if not isinstance(data, dict):
        return []
    return [p[1] for p in data.get("prices", [])]


class CorrelationAgent(BaseAgent):
    NAME = "correlation"
    EMOJI = "🔗"

    def default_weights(self):
        return {
            "correlation_spike": 0.50,
            "correlation_dump": 0.50,
        }

    def run(self, **kwargs):
        days = CORRELATION_WINDOW
        btc_prices = _price_history("bitcoin", days)
        if not btc_prices:
            return []

        signals = []
        for quote, base in CORRELATION_PAIRS:
            base_id = "bitcoin" if base == "BTC" else None
            quote_id = {"ETH": "ethereum", "SOL": "solana", "ADA": "cardano",
                        "AVAX": "avalanche-2", "LINK": "chainlink", "XRP": "ripple"}.get(quote)

            if not quote_id:
                continue
            time.sleep(0.2)
            quote_prices = _price_history(quote_id, days)
            if len(quote_prices) < min(len(btc_prices), 5):
                continue

            n = min(len(btc_prices), len(quote_prices))
            corr = _pearson(btc_prices[-n:], quote_prices[-n:])
            if corr is None:
                continue

            if corr >= CORRELATION_SPIKE_THRESHOLD:
                signals.append(self._make_signal(quote, "correlation_spike", corr))
            elif corr <= CORRELATION_DUMP_THRESHOLD:
                signals.append(self._make_signal(quote, "correlation_dump", corr))

        return signals

    def _make_signal(self, coin: str, signal_name: str, corr: float) -> dict:
        strength = abs(corr)
        confidence = round(min(1.0, 0.5 + (strength - 0.5) * 2), 2) if strength > 0.5 else 0.5
        return {
            "coin": coin,
            "signal": signal_name,
            "confidence": confidence,
            "source": "correlation_monitor",
            "details": {
                "reasons": [
                    f"BTC-{coin} correlation = {corr:.2f}",
                    f"{'decoupling' if signal_name == 'correlation_dump' else 'converging'} market structure",
                ],
                "correlation": round(corr, 3),
                "window_days": CORRELATION_WINDOW,
            },
        }
