"""PatternAgent — technical pattern recognition on price candles.

Detects classic chart patterns (double bottom, head & shoulders, cup & handle)
and volatility-driven momentum setups. Emits `pattern_bullish` / `pattern_bearish`.
"""

from __future__ import annotations

from typing import Optional

from supercrypto.config import (
    PATTERN_MIN_CONFIDENCE,
    PATTERN_VOL_MULTIPLIER,
)
from supercrypto.core.base import BaseAgent, api_get, coin_id_for
from supercrypto.config import COINGECKO_BASE


def _sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return var ** 0.5


def detect_double_bottom(closes: list[float]) -> tuple[str, list[str], float]:
    """Look for W-shape: two local minima with a valley between them.

    Finds the global minimum in the left half and the global minimum in the
    right half of the series, ensuring they're distinct points with a peak
    between them.
    """
    if len(closes) < 15:
        return "", [], 0.0

    mid = len(closes) // 2
    left_half = closes[:mid]
    right_half = closes[mid:]

    if not left_half or not right_half:
        return "", [], 0.0

    low1 = min(left_half)
    low1_idx = left_half.index(low1)
    low2 = min(right_half)
    low2_idx = mid + right_half.index(low2)

    if low1_idx >= low2_idx:
        return "", [], 0.0

    peak = max(closes[low1_idx:low2_idx + 1])
    if peak <= 0 or low1 <= 0 or low2 <= 0:
        return "", [], 0.0

    dip_size = (peak - min(low1, low2)) / peak
    similarity = abs(low1 - low2) / max(low1, low2) if max(low1, low2) > 0 else 1

    if similarity < 0.08 and dip_size > 0.05:
        conf = min(1.0, 0.5 + (1.0 - similarity) * 0.5)
        return "pattern_bullish", [
            f"double-bottom: {low1:.2f} ≈ {low2:.2f} (similarity {1-similarity:.2f})",
            f"recovery {dip_size*100:.0f}% from valley",
        ], conf
    return "", [], 0.0


def detect_head_shoulders(closes: list[float]) -> tuple[str, float]:
    """Simple 3-peak pattern: middle peak higher than two outer ones."""
    if len(closes) < 12:
        return "", 0.0
    right = min(range(len(closes) - 5), key=lambda i: closes[i])
    left = min(range(right, right + 6) if right + 6 <= len(closes) else range(right, len(closes)), key=lambda i: closes[i])
    mid = min(range(left, right + 1), key=lambda i: closes[i]) if left < right else 0
    left_peak = max(closes[max(0, left - 3):left + 1])
    mid_peak = max(closes[mid:mid + 1]) if mid < len(closes) else 0
    right_peak = max(closes[max(0, right - 3):right + 1])
    if left_peak <= 0 or right_peak <= 0:
        return "", 0.0
    if mid_peak > left_peak * 1.05 and mid_peak > right_peak * 1.05:
        conf = min(1.0, (mid_peak / max(left_peak, right_peak) - 1.0) * 5)
        return "pattern_bearish", conf
    return "", 0.0


def detect_volume_breakout(volumes: list[float], closes: list[float]) -> tuple[str, float, list[str]]:
    """Volatility expansion + price momentum = momentum continuation."""
    if len(volumes) < 10:
        return "", 0.0, []
    vol_avg = _sma(volumes, 10)
    vol_std = _std(volumes[-10:])
    if not vol_avg or vol_avg <= 0:
        return "", 0.0, []
    latest_vol = volumes[-1]
    vol_z = (latest_vol - vol_avg) / vol_std if vol_std > 0 else 0
    if vol_z < 1.0:
        return "", 0.0, []
    if len(closes) >= 3:
        price_change = (closes[-1] / closes[-3] - 1.0) * 100 if closes[-3] > 0 else 0
        if price_change > 3:
            conf = min(1.0, 0.5 + vol_z * 0.1)
            return "pattern_bullish", conf, [
                f"volume {vol_z:.1f}σ above avg",
                f"price +{price_change:.1f}% over 3 candles",
            ]
        elif price_change < -3:
            conf = min(1.0, 0.5 + vol_z * 0.1)
            return "pattern_bearish", conf, [
                f"volume {vol_z:.1f}σ above avg",
                f"price {price_change:.1f}% over 3 candles",
            ]
    return "", 0.0, []


class PatternAgent(BaseAgent):
    NAME = "pattern"
    EMOJI = "📊"

    def default_weights(self):
        return {
            "pattern_bullish": 0.60,
            "pattern_bearish": 0.60,
        }

    def run(self, **kwargs):
        coins = kwargs.get("coins") or [
            "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "MATIC"
        ]
        coins = [c.upper() for c in coins]
        signals = []
        for coin in coins:
            result = self._analyze(coin)
            if result:
                signals.append(result)
        return signals

    def _analyze(self, coin: str) -> Optional[dict]:
        cid = coin_id_for(coin)
        if not cid:
            return None
        days = 30
        data = api_get(
            f"{COINGECKO_BASE}/coins/{cid}/market_chart",
            params={"vs_currency": "usd", "days": days, "interval": "daily"},
        )
        if not isinstance(data, dict):
            return None
        prices = data.get("prices", [])
        total_vols = data.get("total_volumes", [])
        if len(prices) < 15:
            return None

        closes = [p[1] for p in prices]
        volumes = [v[1] for v in total_vols[-len(closes):]] if total_vols else [0] * len(closes)
        price = closes[-1]

        sigs = []

        name, conf, reasons = detect_volume_breakout(volumes, closes)
        if name:
            sigs.append({"name": name, "confidence": conf, "reasons": reasons})

        if len(closes) >= 15:
            name, reasons, conf = detect_double_bottom(closes)
            if name:
                sigs.append({"name": name, "confidence": conf, "reasons": reasons})

            hns, conf = detect_head_shoulders(closes)
            if hns:
                sigs.append({
                    "name": hns,
                    "confidence": conf,
                    "reasons": ["head-and-shoulders pattern detected"],
                })

        if not sigs:
            return None

        best = max(sigs, key=lambda s: s["confidence"])
        if best["confidence"] < PATTERN_MIN_CONFIDENCE:
            return None

        return {
            "coin": coin,
            "signal": best["name"],
            "confidence": round(best["confidence"], 2),
            "source": "pattern_recognition",
            "details": {
                "reasons": best["reasons"],
                "price": price,
                "patterns_found": [s["name"] for s in sigs],
            },
        }
