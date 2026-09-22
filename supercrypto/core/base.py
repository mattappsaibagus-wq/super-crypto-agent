"""BaseAgent: signal bus, per-agent memory, dual-horizon outcome settling.

The ox-alpha `_settle_outcomes` (named `learn_from_outcomes` there) used
`pred["coin"].lower()` as a CoinGecko id — BTC became "btc", not "bitcoin" —
so almost no outcomes ever settled. `coin_id_for()` is the fix, plus both
24h and 7d horizons are filled when they mature.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from supercrypto.config import (
    ALL_SIGNALS,
    API_TIMEOUT,
    API_TRIES,
    COINGECKO_BASE,
    DATA_DIR,
    KNOWN_IDS,
    MEMORY_DIR,
    REPORTS_DIR,
    SIGNALS_FILE,
)
from supercrypto.core.learning import WeightLearner


def ensure_dirs() -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def api_get(url: str, params: Optional[dict] = None, tries: int = API_TRIES):
    """GET with backoff on 429 / network errors. Returns parsed JSON or None."""
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=API_TIMEOUT)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                time.sleep(0.5 * (attempt + 1))
                continue
            return r.json()
        except (requests.exceptions.RequestException, ValueError):
            time.sleep(0.5 * (attempt + 1))
    return None


_master_list = None
_ranked_map = None


def coin_master_list() -> list:
    global _master_list
    if _master_list is None:
        data = api_get(f"{COINGECKO_BASE}/coins/list")
        if isinstance(data, list) and data:
            _master_list = data
        else:
            return []
    return _master_list


def ranked_coin_ids() -> dict:
    global _ranked_map
    if _ranked_map is None:
        data = api_get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 200,
                "page": 1,
                "sparkline": "false",
            },
        )
        ranked = {}
        if isinstance(data, list):
            for c in data:
                ranked.setdefault(c["symbol"].upper(), c["id"])
        _ranked_map = ranked
    return _ranked_map


def coin_id_for(symbol: str) -> Optional[str]:
    """Resolve a ticker to a CoinGecko id. Never use symbol.lower() as an id."""
    if not symbol:
        return None
    s = symbol.upper()
    if s in KNOWN_IDS:
        return KNOWN_IDS[s]
    ranked = ranked_coin_ids()
    if s in ranked:
        return ranked[s]
    for c in coin_master_list():
        if c.get("symbol", "").upper() == s:
            return c["id"]
    return None


class BaseAgent:
    NAME = "base"
    EMOJI = "?"
    publish_results = True

    def __init__(self, extra_signals: Optional[set] = None):
        ensure_dirs()
        self.name = self.NAME
        self.emoji = self.EMOJI
        self.memory_path = os.path.join(MEMORY_DIR, f"{self.name}_memory.json")
        self.memory = self._load_memory()
        registry = set(ALL_SIGNALS)
        if extra_signals:
            registry |= set(extra_signals)
        self.learner = WeightLearner(self.memory, registry)
        # Keep subclass default_weights as the starting point for known keys.
        for key, value in self.default_weights().items():
            self.learner.ensure_registered(key)
            if self.memory["weights"].get(key) == self.learner.default_weight:
                self.memory["weights"][key] = value

    def default_weights(self) -> dict:
        return {}

    # ── Signal bus ─────────────────────────────────────────────────────
    def read_signals(self) -> list:
        if not os.path.exists(SIGNALS_FILE):
            return []
        with open(SIGNALS_FILE, "r") as f:
            return json.load(f)

    def write_signals(self, signals: list) -> None:
        existing = self.read_signals()
        seen = {(s.get("coin"), s.get("source", ""), s.get("signal")) for s in existing}
        for sig in signals:
            sig.setdefault("timestamp", now_iso())
            sig.setdefault("agent", self.name)
            key = (sig.get("coin"), sig.get("source", ""), sig.get("signal"))
            if key not in seen:
                existing.append(sig)
                seen.add(key)
        with open(SIGNALS_FILE, "w") as f:
            json.dump(existing, f, indent=2)

    def emit(self, signal: str, confidence: float, coin: str, reason: str = "", **extra) -> dict:
        """Convenience: build + publish a single typed signal."""
        payload = {
            "coin": coin.upper(),
            "signal": signal,
            "confidence": round(confidence, 3),
            "source": extra.pop("source", self.name),
            "details": {"reasons": [reason] if reason else [], **extra},
        }
        if self.publish_results:
            self.write_signals([payload])
        return payload

    def remember(self, key: str, value) -> None:
        self.memory.setdefault("notes", {})[key] = value
        self.save_memory()

    # ── Memory ─────────────────────────────────────────────────────────
    def _load_memory(self) -> dict:
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r") as f:
                return json.load(f)
        return {
            "predictions": [],
            "weights": {},
            "runs": 0,
            "accuracy_history": [],
            "calibration": {"bins": {}, "ece": 0.0, "n": 0},
        }

    def save_memory(self) -> None:
        with open(self.memory_path, "w") as f:
            json.dump(self.memory, f, indent=2)

    def record_prediction(
        self,
        coin: str,
        signal: str,
        confidence: float,
        price_at_prediction: float,
    ) -> None:
        self.learner.ensure_registered(signal)
        taxed = self.learner.taxed_confidence(confidence)
        self.memory["predictions"].append(
            {
                "coin": coin,
                "signal": signal,
                "confidence": taxed,
                "raw_confidence": confidence,
                "price": price_at_prediction,
                "timestamp": now_iso(),
                "outcome_24h": None,
                "outcome_7d": None,
            }
        )
        self.memory["runs"] = self.memory.get("runs", 0) + 1
        self.save_memory()

    def _age_days(self, pred: dict) -> float:
        try:
            ts = datetime.fromisoformat(pred["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            return 0.0
        now = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 86400.0

    def _settle_outcomes(self) -> bool:
        """Fill outcome_24h AND outcome_7d when each horizon has matured.

        Uses coin_id_for() so BTC → bitcoin, not btc. Batches the CoinGecko
        call across all pending coins instead of one request per prediction.
        """
        pending = [
            p
            for p in self.memory.get("predictions", [])
            if p.get("outcome_24h") is None
            or (p.get("outcome_7d") is None and self._age_days(p) >= 7)
        ]
        if not pending:
            return False

        resolved = {}
        for p in pending:
            resolved[p["coin"]] = coin_id_for(p["coin"]) or p["coin"].lower()

        data = (
            api_get(
                f"{COINGECKO_BASE}/simple/price",
                params={
                    "ids": ",".join(sorted(set(resolved.values()))),
                    "vs_currencies": "usd",
                },
            )
            or {}
        )
        if not isinstance(data, dict):
            return False

        changed = False
        for pred in pending:
            cid = resolved[pred["coin"]]
            if cid not in data or not pred.get("price"):
                continue
            try:
                current = data[cid]["usd"]
            except (KeyError, TypeError):
                continue
            ret = (current - pred["price"]) / pred["price"] * 100
            age = self._age_days(pred)
            if pred.get("outcome_24h") is None and age >= 0.9:
                pred["outcome_24h"] = round(ret, 2)
                changed = True
            if pred.get("outcome_7d") is None and age >= 7:
                pred["outcome_7d"] = round(ret, 2)
                changed = True
        return changed

    def learn_from_outcomes(self) -> None:
        changed = self._settle_outcomes()
        settled = [
            p
            for p in self.memory.get("predictions", [])
            if p.get("outcome_24h") is not None or p.get("outcome_7d") is not None
        ]
        if settled:
            self.learner.update_weights(settled)
            self.learner.update_calibration(settled)
        if changed or settled:
            self.save_memory()

    # ── Run ────────────────────────────────────────────────────────────
    def run(self, **kwargs) -> list:
        raise NotImplementedError

    def execute(self, **kwargs) -> list:
        print(f"  {self.emoji} {self.name} — scanning...")
        try:
            signals = self.run(**kwargs) or []
            if signals and self.publish_results:
                self.write_signals(signals)
                for s in signals:
                    price = (s.get("details") or {}).get("price")
                    if s.get("confidence", 0) >= 0.6 and price:
                        try:
                            self.record_prediction(
                                s["coin"], s["signal"], s["confidence"], price
                            )
                        except KeyError:
                            # Unregistered signal: skip learning, still emit.
                            pass
            self.learn_from_outcomes()
            print(f"  {self.emoji} {self.name} — found {len(signals)} signal(s)")
            return signals
        except Exception as e:
            print(f"  {self.emoji} {self.name} — ERROR: {e}")
            return []
