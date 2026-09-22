"""MacroRegime — Fear & Greed + BTC dominance → risk_on / neutral / risk_off.

Gates advisor aggressiveness. A coin can look great in isolation while the
whole market is risk-off; this is the check ox-alpha never had.
"""

from __future__ import annotations

import json
import os

from supercrypto.config import (
    COINGECKO_GLOBAL,
    DATA_DIR,
    FEAR_GREED_URL,
)
from supercrypto.core.base import BaseAgent, api_get
from supercrypto.core.scoring import classify_regime


REGIME_FILE = os.path.join(DATA_DIR, "regime.json")


class MacroRegime(BaseAgent):
    NAME = "macro"
    EMOJI = "🌐"

    def default_weights(self):
        return {
            "regime_risk_on": 0.6,
            "regime_neutral": 0.5,
            "regime_risk_off": 0.6,
        }

    def run(self, **kwargs):
        fng = self._fear_greed()
        dominance = self._btc_dominance()
        regime = classify_regime(fng, dominance)
        payload = {
            "regime": regime,
            "fear_greed": fng,
            "btc_dominance": dominance,
        }
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(REGIME_FILE, "w") as f:
            json.dump(payload, f, indent=2)

        signal = "regime_{}".format(regime)
        conf = 0.7 if fng is not None else 0.4
        return [
            {
                "coin": "BTC",
                "signal": signal,
                "confidence": conf,
                "source": "macro",
                "details": {
                    "reasons": [
                        "Fear&Greed={} BTC.D={}".format(
                            fng if fng is not None else "n/a",
                            "{:.1f}%".format(dominance) if dominance is not None else "n/a",
                        )
                    ],
                    "regime": regime,
                    "fear_greed": fng,
                    "btc_dominance": dominance,
                },
            }
        ]

    @staticmethod
    def current_regime() -> str:
        if os.path.exists(REGIME_FILE):
            try:
                with open(REGIME_FILE) as f:
                    return json.load(f).get("regime") or "neutral"
            except (OSError, ValueError):
                return "neutral"
        return "neutral"

    def _fear_greed(self):
        data = api_get(FEAR_GREED_URL, params={"limit": 1})
        try:
            return int(data["data"][0]["value"])
        except (TypeError, KeyError, IndexError, ValueError):
            return None

    def _btc_dominance(self):
        data = api_get(COINGECKO_GLOBAL)
        try:
            return float(data["data"]["market_cap_percentage"]["btc"])
        except (TypeError, KeyError, ValueError):
            return None
