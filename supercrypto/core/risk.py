"""Risk manager: position caps, DD-scaled sizing, exposure limits, circuit breaker.

Can veto or downgrade a BUY. Never invents a BUY that the advisor did not emit.
"""

from __future__ import annotations

from typing import Optional

from supercrypto.config import (
    DEFAULT_CIRCUIT_BREAKER_DD_PCT,
    DEFAULT_MAX_CONCURRENT_BUYS,
    DEFAULT_MAX_POSITION_PCT,
    DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
)


class RiskManager:
    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        risk = cfg.get("risk") or cfg
        self.max_position_pct = float(
            risk.get("max_position_pct", DEFAULT_MAX_POSITION_PCT)
        )
        self.max_total_exposure_pct = float(
            risk.get("max_total_exposure_pct", DEFAULT_MAX_TOTAL_EXPOSURE_PCT)
        )
        self.max_concurrent_buys = int(
            risk.get("max_concurrent_buys", DEFAULT_MAX_CONCURRENT_BUYS)
        )
        self.circuit_breaker_dd_pct = float(
            risk.get(
                "circuit_breaker_drawdown_pct",
                risk.get("circuit_breaker_dd_pct", DEFAULT_CIRCUIT_BREAKER_DD_PCT),
            )
        )
        self.stop_loss_pct = float(risk.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT))
        self.take_profit_pct = float(
            risk.get("take_profit_pct", DEFAULT_TAKE_PROFIT_PCT)
        )

    def _current_drawdown_pct(self, paper) -> float:
        if paper is None:
            return 0.0
        state = getattr(paper, "state", None) or {}
        peak = state.get("peak_equity") or state.get("starting_equity") or 0
        equity = state.get("equity") or 0
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - equity) / peak * 100.0)

    def _open_exposure_pct(self, paper) -> float:
        if paper is None:
            return 0.0
        state = getattr(paper, "state", None) or {}
        equity = state.get("equity") or 0
        if equity <= 0:
            return 0.0
        positions = state.get("positions") or {}
        notional = 0.0
        for pos in positions.values():
            size = pos.get("size_pct") or 0
            notional += size
        return notional

    def _open_count(self, paper) -> int:
        if paper is None:
            return 0
        state = getattr(paper, "state", None) or {}
        return len(state.get("positions") or {})

    def size_for(self, dd: Optional[float], action: str) -> float:
        """DD-scaled position size. Weak DD → smaller size, never above the cap."""
        if action != "BUY":
            return 0.0
        strength = 0.5 if dd is None else max(0.0, min(1.0, dd))
        return round(self.max_position_pct * strength, 3)

    def adjust(self, verdicts: list, paper=None) -> list:
        """Return a new list of verdicts. Original dicts are copied, not mutated."""
        breaker_tripped = (
            self._current_drawdown_pct(paper) >= self.circuit_breaker_dd_pct
        )
        open_count = self._open_count(paper)
        used_exposure = self._open_exposure_pct(paper)
        remaining_slots = max(0, self.max_concurrent_buys - open_count)
        remaining_exposure = max(0.0, self.max_total_exposure_pct - used_exposure)

        out = []
        buys_this_pass = 0
        exposure_this_pass = 0.0
        for raw in verdicts:
            v = dict(raw)
            notes = list(v.get("risk_notes") or [])
            action = v.get("action")
            dd = v.get("dd")

            if action == "BUY":
                if breaker_tripped:
                    v["action"] = "WATCH"
                    notes.append(
                        "CIRCUIT BREAKER: drawdown exceeds {:.0f}% — new buys blocked".format(
                            self.circuit_breaker_dd_pct
                        )
                    )
                elif buys_this_pass >= remaining_slots:
                    v["action"] = "WATCH"
                    notes.append(
                        "concurrent-buy cap ({}) reached".format(self.max_concurrent_buys)
                    )
                else:
                    size = self.size_for(dd, "BUY")
                    headroom = remaining_exposure - exposure_this_pass
                    if headroom <= 0:
                        v["action"] = "WATCH"
                        notes.append("total exposure cap reached")
                    else:
                        size = min(size, headroom)
                        v["suggested_size_pct"] = size
                        v["stop_loss_pct"] = self.stop_loss_pct
                        v["take_profit_pct"] = self.take_profit_pct
                        buys_this_pass += 1
                        exposure_this_pass += size

            if v.get("action") != "BUY":
                v.setdefault("suggested_size_pct", 0.0)

            v["risk_notes"] = notes
            out.append(v)
        return out
