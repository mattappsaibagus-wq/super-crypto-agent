"""On-chain holder concentration via free Etherscan API (optional API key).

Falls back gracefully: if no key or unsupported chain, emits nothing and the
DD agent's DEX liquidity-spread proxy remains the concentration signal.
"""

from __future__ import annotations

import json
import os

from supercrypto.config import DATA_DIR, ETHERSCAN_API
from supercrypto.core.base import BaseAgent, api_get


ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
CONTRACT_MAP_FILE = os.path.join(DATA_DIR, "token_contracts.json")

# Small built-in map; extend via data/token_contracts.json
DEFAULT_CONTRACTS = {
    "shib": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",
    "pepe": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
    "link": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "uni": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
}


class OnChainHolder(BaseAgent):
    NAME = "onchain"
    EMOJI = "⛓️"

    def default_weights(self):
        return {
            "holder_red_flag": 0.70,
            "holder_healthy": 0.40,
        }

    def run(self, **kwargs):
        if not ETHERSCAN_KEY:
            return []
        coins = kwargs.get("coins")
        if not coins:
            coins = sorted({s.get("coin") for s in self.read_signals() if s.get("coin")})
        signals = []
        for sym in coins:
            addr = self._contract_for(sym)
            if not addr:
                continue
            data = api_get(
                ETHERSCAN_API,
                params={
                    "module": "token",
                    "action": "topholders",
                    "contractaddress": addr,
                    "page": 1,
                    "offset": 10,
                    "apikey": ETHERSCAN_KEY,
                },
            ) or {}
            holders = data.get("result") if isinstance(data.get("result"), list) else []
            if not holders:
                continue
            try:
                balances = [float(h.get("balance", 0) or 0) for h in holders]
            except (TypeError, ValueError):
                continue
            total = sum(balances)
            if total <= 0:
                continue
            share = sum(balances[:10]) / total
            note = "top10 hold {:.0f}% of supply".format(share * 100)
            if share > 0.5:
                signals.append(
                    {
                        "coin": sym.upper(),
                        "signal": "holder_red_flag",
                        "confidence": 0.7,
                        "source": "etherscan",
                        "details": {
                            "reasons": [note],
                            "top10_holder_share": round(share, 3),
                        },
                    }
                )
            else:
                signals.append(
                    {
                        "coin": sym.upper(),
                        "signal": "holder_healthy",
                        "confidence": 0.5,
                        "source": "etherscan",
                        "details": {
                            "reasons": [note],
                            "top10_holder_share": round(share, 3),
                        },
                    }
                )
            self.remember(
                "holders:{}".format(sym.upper()),
                {"share": round(share, 3), "n": len(holders)},
            )
        return signals

    def _contract_for(self, sym: str):
        mapping = dict(DEFAULT_CONTRACTS)
        if os.path.exists(CONTRACT_MAP_FILE):
            try:
                with open(CONTRACT_MAP_FILE) as f:
                    mapping.update({k.lower(): v for k, v in json.load(f).items()})
            except (OSError, ValueError):
                pass
        return mapping.get(sym.lower())
