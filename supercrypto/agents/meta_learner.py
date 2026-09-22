"""MetaLearner — the self-improvement layer.

Monitors aggregate agent performance, detects regime shifts, and adjusts
agent priority weights based on recent accuracy. Emits `meta_agent_trusted`
and `meta_agent_deprioritized` signals that the advisor uses for weight
multipliers.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Optional

from supercrypto.agents.macro import REGIME_FILE
from supercrypto.config import (
    DATA_DIR,
    META_AGENT_PENALTY,
    META_MIN_SAMPLES,
    META_REGIME_SWITCH_THRESHOLD,
)
from supercrypto.core.base import BaseAgent


META_STATE_FILE = os.path.join(DATA_DIR, "meta_state.json")


class MetaLearner(BaseAgent):
    NAME = "meta"
    EMOJI = "🧠"
    publish_results = False

    def default_weights(self):
        return {
            "meta_agent_trusted": 0.60,
            "meta_agent_deprioritized": 0.60,
        }

    def run(self, **kwargs):
        agents = self._load_agent_memories()
        if not agents:
            return []

        verdicts = []
        regime = self._current_regime()
        recent_regime = self.memory.get("last_regime", regime)

        # Check for regime switch
        if recent_regime != regime:
            self.memory["regime_switches"] = self.memory.get("regime_switches", 0) + 1
            self.remember("last_regime", regime)

        for agent_name, mem in agents.items():
            stats = self._agent_stats(agent_name, mem, regime)
            if not stats:
                continue
            agent_weight = self.learner.agent_weight_adjustment(agent_name, regime)

            if stats["win_rate"] >= 0.55 and stats["samples"] >= META_MIN_SAMPLES:
                verdicts.append(self._emit_trusted(agent_name, agent_weight, stats))
            elif stats["win_rate"] < 0.45 and stats["samples"] >= META_MIN_SAMPLES:
                verdicts.append(self._emit_deprioritized(agent_name, agent_weight, stats))

        # Write signals to bus
        validated = []
        for v in verdicts:
            self.learner.ensure_registered(v["signal"])
            val, _, _ = self._validate_and_emit(v)
            if val:
                validated.append(val)

        self._write_state(agents, verdicts)
        return validated

    def _load_agent_memories(self) -> dict:
        from supercrypto.config import MEMORY_DIR

        result = {}
        for name in ["whale", "sentiment", "pattern", "correlation", "news", "dd", "macro"]:
            path = os.path.join(MEMORY_DIR, f"{name}_memory.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    result[name] = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        return result

    def _agent_stats(self, name: str, mem: dict, regime: str) -> Optional[dict]:
        predictions = mem.get("predictions", [])
        settled = [
            p for p in predictions
            if p.get("outcome_24h") is not None or p.get("outcome_7d") is not None
        ]
        if len(settled) < META_MIN_SAMPLES:
            return None

        from supercrypto.core.learning import WeightLearner
        learner = WeightLearner(mem)
        outcomes = []
        wins = 0
        for p in settled:
            blend = learner.blended_outcome(p)
            if blend is None:
                continue
            outcomes.append(blend)
            if blend > 0:
                wins += 1

        n = len(outcomes)
        if n == 0:
            return None
        avg_ret = sum(outcomes) / n
        win_rate = wins / n

        return {
            "samples": n,
            "win_rate": round(win_rate, 3),
            "avg_return": round(avg_ret, 2),
        }

    def _current_regime(self) -> str:
        if os.path.exists(REGIME_FILE):
            try:
                with open(REGIME_FILE) as f:
                    return json.load(f).get("regime", "neutral")
            except (OSError, json.JSONDecodeError):
                pass
        return "neutral"

    def _emit_trusted(self, agent: str, weight_mult: float, stats: dict) -> dict:
        return {
            "coin": "ALL",
            "signal": "meta_agent_trusted",
            "confidence": round(0.5 + weight_mult * 0.5, 2),
            "source": "meta_learner",
            "target_agent": agent,
            "weight_multiplier": weight_mult,
            "details": {
                "reasons": [
                    f"{agent} trusted (win_rate={stats['win_rate']:.1%}, "
                    f"samples={stats['samples']})",
                    f"weight multiplier: {weight_mult}x",
                ],
                "agent": agent,
                "win_rate": stats["win_rate"],
                "samples": stats["samples"],
            },
        }

    def _emit_deprioritized(self, agent: str, weight_mult: float, stats: dict) -> dict:
        penalty = META_AGENT_PENALTY
        return {
            "coin": "ALL",
            "signal": "meta_agent_deprioritized",
            "confidence": round(0.5 + (1.0 - weight_mult) * 0.5, 2),
            "source": "meta_learner",
            "target_agent": agent,
            "weight_multiplier": round(weight_mult - penalty, 2),
            "details": {
                "reasons": [
                    f"{agent} deprioritized (win_rate={stats['win_rate']:.1%}, "
                    f"samples={stats['samples']})",
                    f"applying {penalty*100}% penalty",
                ],
                "agent": agent,
                "win_rate": stats["win_rate"],
                "samples": stats["samples"],
            },
        }

    def _validate_and_emit(self, signal: dict) -> tuple[Optional[dict], str, list]:
        self.learner.ensure_registered(signal["signal"])
        taxed = self.learner.taxed_confidence(signal["confidence"])
        signal["confidence"] = taxed
        return signal, "", []

    def _write_state(self, agents: dict, verdicts: list) -> None:
        state = {
            "agents_tracked": list(agents.keys()),
            "verdicts": len(verdicts),
            "total_switches": self.memory.get("regime_switches", 0),
        }
        self.remember("meta_state", state)
        self.save_memory()
