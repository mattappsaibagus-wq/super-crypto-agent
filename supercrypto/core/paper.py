"""Paper trader: simulated entries/exits, equity curve, win rate, max drawdown.

`path=None` keeps state in memory (used by the portfolio backtest runner so it
never touches the live paper file).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from supercrypto.config import PAPER_FILE, STARTING_EQUITY


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperTrader:
    def __init__(self, path: Optional[str] = PAPER_FILE, starting_equity: float = STARTING_EQUITY):
        self.path = path
        self.starting_equity = starting_equity
        self.state = self._load()

    def _empty(self) -> dict:
        return {
            "equity": self.starting_equity,
            "peak_equity": self.starting_equity,
            "starting_equity": self.starting_equity,
            "cash": self.starting_equity,
            "positions": {},
            "closed": [],
            "equity_curve": [{"t": _now(), "equity": self.starting_equity}],
        }

    def _load(self) -> dict:
        if self.path and os.path.exists(self.path):
            with open(self.path) as f:
                state = json.load(f)
            state.setdefault("peak_equity", state.get("equity", self.starting_equity))
            state.setdefault("starting_equity", self.starting_equity)
            state.setdefault("cash", state.get("equity", self.starting_equity))
            state.setdefault("positions", {})
            state.setdefault("closed", [])
            state.setdefault("equity_curve", [])
            return state
        return self._empty()

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.state, f, indent=2)

    def _mark_equity(self, prices: dict) -> float:
        equity = self.state["cash"]
        for coin, pos in self.state["positions"].items():
            px = prices.get(coin)
            if px is None:
                px = pos.get("last_price") or pos["entry_price"]
            pos["last_price"] = px
            equity += pos["qty"] * px
        self.state["equity"] = round(equity, 4)
        if equity > self.state["peak_equity"]:
            self.state["peak_equity"] = round(equity, 4)
        self.state["equity_curve"].append({"t": _now(), "equity": round(equity, 4)})
        # Keep the curve from growing without bound.
        if len(self.state["equity_curve"]) > 2000:
            self.state["equity_curve"] = self.state["equity_curve"][-1500:]
        return equity

    def _close(self, coin: str, price: float, reason: str) -> None:
        pos = self.state["positions"].pop(coin)
        proceeds = pos["qty"] * price
        self.state["cash"] += proceeds
        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        self.state["closed"].append(
            {
                "coin": coin,
                "entry_price": pos["entry_price"],
                "exit_price": price,
                "qty": pos["qty"],
                "size_pct": pos.get("size_pct"),
                "pnl_pct": round(pnl_pct, 3),
                "exit_reason": reason,
                "opened_at": pos.get("opened_at"),
                "closed_at": _now(),
            }
        )

    def process(self, verdicts: list, prices: dict) -> dict:
        """Mark-to-market, honor stops/targets, then open new BUYs."""
        # 1. Exits on existing positions.
        to_close = []
        for coin, pos in list(self.state["positions"].items()):
            px = prices.get(coin)
            if px is None:
                continue
            sl = pos.get("stop_loss_pct") or 0
            tp = pos.get("take_profit_pct") or 0
            change = (px - pos["entry_price"]) / pos["entry_price"] * 100
            if sl and change <= -abs(sl):
                to_close.append((coin, px, "stop_loss"))
            elif tp and change >= abs(tp):
                to_close.append((coin, px, "take_profit"))
        for coin, px, reason in to_close:
            self._close(coin, px, reason)

        # 2. New entries.
        equity_before = self._mark_equity(prices)
        for v in verdicts:
            if v.get("action") != "BUY":
                continue
            coin = v["coin"]
            if coin in self.state["positions"]:
                continue
            px = prices.get(coin)
            if not px or px <= 0:
                continue
            size_pct = float(v.get("suggested_size_pct") or 0)
            if size_pct <= 0:
                continue
            notional = equity_before * (size_pct / 100.0)
            if notional > self.state["cash"]:
                notional = self.state["cash"]
            if notional <= 0:
                continue
            qty = notional / px
            self.state["cash"] -= notional
            self.state["positions"][coin] = {
                "entry_price": px,
                "qty": qty,
                "size_pct": size_pct,
                "stop_loss_pct": v.get("stop_loss_pct"),
                "take_profit_pct": v.get("take_profit_pct"),
                "opened_at": _now(),
                "last_price": px,
            }

        self._mark_equity(prices)
        self.save()
        return self.metrics()

    def metrics(self) -> dict:
        closed = self.state.get("closed") or []
        wins = [t for t in closed if (t.get("pnl_pct") or 0) > 0]
        peak = self.state.get("peak_equity") or self.state.get("equity") or 0
        equity = self.state.get("equity") or 0
        max_dd = 0.0
        running_peak = self.state.get("starting_equity") or STARTING_EQUITY
        for pt in self.state.get("equity_curve") or []:
            eq = pt.get("equity") or 0
            if eq > running_peak:
                running_peak = eq
            if running_peak > 0:
                dd = (running_peak - eq) / running_peak * 100
                if dd > max_dd:
                    max_dd = dd
        if peak > 0:
            live_dd = (peak - equity) / peak * 100
            max_dd = max(max_dd, live_dd)
        win_rate = round(len(wins) / len(closed) * 100, 1) if closed else None
        return {
            "current_equity": round(equity, 2),
            "open_positions": len(self.state.get("positions") or {}),
            "closed_trades": len(closed),
            "win_rate": win_rate,
            "max_drawdown_pct": round(max_dd, 2),
        }
