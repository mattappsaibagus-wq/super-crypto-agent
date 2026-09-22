"""Enhanced WeightLearner for Super Crypto Agent.

Extends alpha-forge's learning engine with:
- Regime-aware weights (risk_on / neutral / risk_off get separate weight tracks)
- Cross-agent knowledge sharing (agents read each other's learned accuracy)
- Multi-dimensional rewards (return + Sharpe + drawdown-adjusted)
- Agent performance attribution tracking
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Mapping, Optional

from supercrypto.config import (
    ALL_SIGNALS,
    DEFAULT_SIGNAL_WEIGHT,
    EMA_ALPHA,
    HORIZON_WEIGHTS,
    MAX_WEIGHT,
    MIN_SAMPLES,
    MIN_WEIGHT,
    META_AGENT_PENALTY,
    META_MIN_SAMPLES,
    META_REGIME_SWITCH_THRESHOLD,
)


def magnitude_reward(pct_return: float, scale: float = 10.0) -> float:
    if pct_return is None:
        return 0.0
    return math.tanh(pct_return / scale)


def sharpe_reward(returns: list[float], rf: float = 0.0) -> float:
    """Annualized Sharpe-like reward from a series of percentage returns."""
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    if len(returns) < 2:
        return magnitude_reward(mean)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return magnitude_reward(mean)
    sharpe = (mean - rf) / std * math.sqrt(365)
    return math.tanh(sharpe / 2.0)


def drawdown_adjusted_reward(pct_return: float, max_dd_pct: float) -> float:
    """Return component penalized by the drawdown experienced on this signal."""
    if max_dd_pct >= 20.0:
        return magnitude_reward(pct_return) * 0.5
    if max_dd_pct >= 10.0:
        return magnitude_reward(pct_return) * 0.75
    return magnitude_reward(pct_return)


class WeightLearner:
    """Owns `memory['weights']` and `memory['calibration']`. Mutates in place.

    Super Crypto Agent enhancements:
    - Regime-aware weight tracks: weights stored per-regime so risk_on signals
      aren't penalized just because risk_off made them look bad.
    - Agent performance registry: tracks which agent each signal comes from,
      enabling cross-agent knowledge sharing.
    - Multi-dimensional reward: blends return + Sharpe + drawdown-adjusted.
    """

    REGIMES = ("risk_on", "neutral", "risk_off")

    def __init__(
        self,
        memory: dict,
        registry: Iterable[str] = ALL_SIGNALS,
        ema_alpha: float = EMA_ALPHA,
        min_samples: int = MIN_SAMPLES,
        min_weight: float = MIN_WEIGHT,
        max_weight: float = MAX_WEIGHT,
        default_weight: float = DEFAULT_SIGNAL_WEIGHT,
    ):
        self.memory = memory
        self.registry = set(registry)
        self.ema_alpha = ema_alpha
        self.min_samples = min_samples
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.default_weight = default_weight

        weights = memory.setdefault("weights", {})
        for key in self.registry:
            weights.setdefault(key, default_weight)

        # Enhanced: regime-aware weight tracks
        self.regime_weights = memory.setdefault("regime_weights", {})
        for regime in self.REGIMES:
            rw = self.regime_weights.setdefault(regime, {})
            for key in self.registry:
                rw.setdefault(key, default_weight)

        # Enhanced: agent performance tracking
        memory.setdefault("agent_stats", {})

        memory.setdefault("calibration", {"bins": {}, "ece": 0.0, "n": 0})
        memory.setdefault("accuracy_history", [])

    def ensure_registered(self, signal: str) -> None:
        if signal not in self.registry:
            raise KeyError(
                f"unregistered signal {signal!r}; add it to ALL_SIGNALS / the "
                f"agent registry before recording a prediction"
            )
        self.memory["weights"].setdefault(signal, self.default_weight)
        for regime in self.REGIMES:
            self.regime_weights[regime].setdefault(signal, self.default_weight)

    @staticmethod
    def blended_outcome(
        pred: Mapping,
        horizon_weights: Mapping[str, float] = HORIZON_WEIGHTS,
    ) -> Optional[float]:
        parts = []
        total_w = 0.0
        for horizon, weight in horizon_weights.items():
            key = f"outcome_{horizon}"
            value = pred.get(key)
            if value is not None:
                parts.append(value * weight)
                total_w += weight
        if total_w == 0:
            return None
        return sum(parts) / total_w

    def _multi_dim_reward(self, pred: Mapping) -> tuple[float, dict]:
        """Compute a blended reward from return, Sharpe, and drawdown-adjusted."""
        blend = self.blended_outcome(pred)
        if blend is None:
            return 0.0, {}

        ret_reward = magnitude_reward(blend)
        dd = pred.get("max_drawdown_pct", 0)
        dd_reward = drawdown_adjusted_reward(blend, dd)

        # Use returns history if available for Sharpe
        returns = pred.get("return_history", [])
        if returns:
            sharpe = sharpe_reward(returns)
        else:
            sharpe = ret_reward

        components = {
            "return": round(ret_reward, 4),
            "sharpe": round(sharpe, 4),
            "dd_adjusted": round(dd_reward, 4),
        }
        # Primary: return reward. Secondary: Sharpe. Adjusted by drawdown.
        composite = 0.5 * ret_reward + 0.3 * sharpe + 0.2 * dd_reward
        return composite, components

    def get_weight(self, signal: str, regime: str = "neutral") -> float:
        """Get the weight for a signal, regime-aware if available."""
        rw = self.regime_weights.get(regime, {})
        return rw.get(signal, self.memory["weights"].get(signal, self.default_weight))

    def update_weights(self, settled: Iterable[Mapping]) -> None:
        """EMA-blend weights using multi-dimensional rewards.

        Updates both global weights and regime-specific tracks.
        """
        by_signal: dict[str, list[tuple[float, str]]] = defaultdict(list)
        agent_returns: dict[str, list[float]] = defaultdict(list)

        for pred in settled:
            signal = pred.get("signal")
            if not signal or signal not in self.registry:
                continue
            blend = self.blended_outcome(pred)
            if blend is None:
                continue

            composite, _ = self._multi_dim_reward(pred)
            regime = pred.get("regime", "neutral")
            by_signal[signal].append((composite, regime))

            agent = pred.get("agent", "unknown")
            agent_returns[agent].append(blend)

        weights = self.memory["weights"]
        regime_weights = self.memory["regime_weights"]

        for signal, reward_regimes in by_signal.items():
            rewards_only = [r for r, _ in reward_regimes]
            if len(rewards_only) < self.min_samples:
                continue
            mean_reward = sum(rewards_only) / len(rewards_only)
            target = 0.5 + 0.5 * mean_reward
            old = weights.get(signal, self.default_weight)
            updated = old * (1.0 - self.ema_alpha) + target * self.ema_alpha
            weights[signal] = round(
                max(self.min_weight, min(self.max_weight, updated)), 4
            )

            # Per-regime updates
            for regime in self.REGIMES:
                regime_rewards = [r for r, rg in reward_regimes if rg == regime]
                if len(regime_rewards) < self.min_samples:
                    continue
                regime_mean = sum(regime_rewards) / len(regime_rewards)
                regime_target = 0.5 + 0.5 * regime_mean
                r_old = regime_weights[regime].get(signal, self.default_weight)
                r_updated = r_old * (1.0 - self.ema_alpha) + regime_target * self.ema_alpha
                regime_weights[regime][signal] = round(
                    max(self.min_weight, min(self.max_weight, r_updated)), 4
                )

        # Update agent performance stats for meta-learning
        agent_stats = self.memory["agent_stats"]
        for agent, returns in agent_returns.items():
            if len(returns) < 1:
                continue
            wins = sum(1 for r in returns if r > 0)
            win_rate = wins / len(returns) if returns else 0
            avg_ret = sum(returns) / len(returns)
            stats = agent_stats.setdefault(agent, {
                "samples": 0, "wins": 0, "avg_return": 0.0, "win_rate": 0.0
            })
            stats["samples"] = stats.get("samples", 0) + len(returns)
            stats["wins"] = stats.get("wins", 0) + wins
            stats["win_rate"] = round(stats["wins"] / stats["samples"], 3)
            stats["avg_return"] = round(
                (stats.get("avg_return", 0) * (stats["samples"] - len(returns)) + avg_ret * len(returns)) / stats["samples"],
                3
            )

        self.memory.setdefault("accuracy_history", []).append(
            {
                "weights": dict(weights),
                "sample_size": sum(len(v) for v in by_signal.values()),
            }
        )

    def update_calibration(self, settled: Iterable[Mapping], n_bins: int = 5) -> float:
        bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
        for pred in settled:
            conf = pred.get("confidence")
            blend = self.blended_outcome(pred)
            if conf is None or blend is None:
                continue
            hit = 1 if blend > 0 else 0
            bin_idx = min(n_bins - 1, int(conf * n_bins))
            bins[bin_idx].append((conf, hit))

        total = sum(len(v) for v in bins.values())
        if total == 0:
            return 0.0

        ece = 0.0
        serialized = {}
        for idx, items in bins.items():
            n = len(items)
            mean_conf = sum(c for c, _ in items) / n
            mean_acc = sum(h for _, h in items) / n
            ece += (n / total) * abs(mean_conf - mean_acc)
            serialized[str(idx)] = {
                "n": n,
                "mean_conf": round(mean_conf, 4),
                "mean_acc": round(mean_acc, 4),
            }

        self.memory["calibration"] = {
            "bins": serialized,
            "ece": round(ece, 4),
            "n": total,
        }
        return round(ece, 4)

    def confidence_tax(self) -> float:
        ece = (self.memory.get("calibration") or {}).get("ece") or 0.0
        return min(0.25, max(0.0, ece))

    def taxed_confidence(self, raw: float) -> float:
        tax = self.confidence_tax()
        return round(max(0.0, min(1.0, raw * (1.0 - tax))), 3)

    def agent_weight_adjustment(self, agent: str, regime: str = "neutral") -> float:
        """Return a multiplier [0.5, 1.5] based on this agent's historical performance.

        Used by the advisor to boost trustworthy agents and deprioritize weak ones.
        """
        stats = self.memory.get("agent_stats", {}).get(agent, {})
        if not stats or stats.get("samples", 0) < META_MIN_SAMPLES:
            return 1.0
        win_rate = stats.get("win_rate", 0.5)
        adj = 0.5 + win_rate
        return round(max(0.5, min(1.5, adj)), 2)

    def regime_weight(self, signal: str, regime: str = "neutral") -> float:
        """Get the regime-specific weight for a signal."""
        rw = self.regime_weights.get(regime, {})
        return rw.get(signal, self.memory["weights"].get(signal, self.default_weight))

    def cross_agent_share(self, target_agent: str, best_signals: list[str], top_n: int = 3) -> None:
        """Share the top-N signals from a high-performing agent to another.

        Boosts the target agent's weights on signals that have proven profitable.
        """
        target_weights = self.memory["weights"]
        for sig in best_signals[:top_n]:
            if sig in self.registry:
                current = target_weights.get(sig, self.default_weight)
                target_weights[sig] = round(min(self.max_weight, current * 1.1), 4)
        self.memory["weights"] = target_weights
