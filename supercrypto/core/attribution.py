"""Attribution engine: decomposes portfolio P&L back to signal sources.

Tracks which agents and which signal types contribute most to profitable
(trade-closing) outcomes, and flags signals whose live accuracy has drifted
below their calibrated confidence.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Optional

from supercrypto.config import DATA_DIR, REPORTS_DIR


ATTRIB_DIR = os.path.join(DATA_DIR, "attribution")
ATTRIB_FILE = os.path.join(ATTRIB_DIR, "attribution.json")
ATTRIB_REPORT = os.path.join(REPORTS_DIR, "last_attribution.md")


class AttributionEngine:
    def __init__(self, path: Optional[str] = ATTRIB_FILE):
        self.path = path or ATTRIB_FILE
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.history: list[dict] = self._load()

    def _load(self) -> list:
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        return []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.history, f, indent=2)

    def record_trade(
        self,
        coin: str,
        entry_price: float,
        exit_price: float,
        exit_reason: str,
        contributors: list[dict],
    ) -> None:
        """Record a closed trade with the signals that contributed.

        Each entry in `contributors` looks like:
          {"signal": "whale_up", "agent": "whale", "confidence": 0.7, "weight": 0.3}
        """
        if entry_price <= 0:
            return
        pnl_pct = (exit_price / entry_price - 1.0) * 100
        ts = __import__("supercrypto.core.base", fromlist=["now_iso"]).now_iso()
        self.history.append(
            {
                "timestamp": ts,
                "coin": coin.upper(),
                "entry_price": round(entry_price, 6),
                "exit_price": round(exit_price, 6),
                "pnl_pct": round(pnl_pct, 3),
                "exit_reason": exit_reason,
                "contributors": [
                    {
                        "signal": c.get("signal", ""),
                        "agent": c.get("agent", ""),
                        "confidence": c.get("confidence", 0),
                        "weight": c.get("weight", 0.5),
                    }
                    for c in contributors
                ],
            }
        )
        if len(self.history) > 2000:
            self.history = self.history[-1500:]
        self.save()

    def agent_summary(self) -> list[dict]:
        """Return sorted list of agent attribution stats."""
        by_agent: dict[str, dict] = defaultdict(
            lambda: {"n_trades": 0, "pnl_total": 0.0, "pnl_wins": 0.0, "conf_sum": 0.0, "conf_n": 0}
        )
        for t in self.history:
            for c in t.get("contributors", []):
                agent = c.get("agent") or "unknown"
                stats = by_agent[agent]
                stats["n_trades"] += 1
                stats["pnl_total"] += t["pnl_pct"]
                if t["pnl_pct"] > 0:
                    stats["pnl_wins"] += 1
                conf = c.get("confidence", 0)
                if conf:
                    stats["conf_sum"] += conf
                    stats["conf_n"] += 1

        rows = []
        for agent, s in by_agent.items():
            n = s["n_trades"]
            avg = s["pnl_total"] / n if n else 0
            win_rate = (s["pnl_wins"] / n * 100) if n else 0
            avg_conf = (s["conf_sum"] / s["conf_n"]) if s["conf_n"] else 0
            rows.append(
                {
                    "agent": agent,
                    "n_trades": n,
                    "avg_pnl_pct": round(avg, 2),
                    "win_rate": round(win_rate, 1),
                    "avg_confidence": round(avg_conf, 3),
                }
            )
        rows.sort(key=lambda r: r["avg_pnl_pct"], reverse=True)
        return rows

    def signal_summary(self) -> list[dict]:
        """Return sorted list of per-signal attribution stats."""
        by_signal: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "pnl": 0.0, "wins": 0}
        )
        for t in self.history:
            for c in t.get("contributors", []):
                sig = c.get("signal") or "unknown"
                stats = by_signal[sig]
                stats["n"] += 1
                stats["pnl"] += t["pnl_pct"]
                if t["pnl_pct"] > 0:
                    stats["wins"] += 1

        rows = []
        for sig, s in by_signal.items():
            n = s["n"]
            rows.append(
                {
                    "signal": sig,
                    "n": n,
                    "avg_pnl_pct": round(s["pnl"] / n, 2) if n else 0,
                    "win_rate": round(s["wins"] / n * 100, 1) if n else 0,
                }
            )
        rows.sort(key=lambda r: r["avg_pnl_pct"], reverse=True)
        return rows

    def overfit_signals(self, weight_threshold: float = 0.7, accuracy_threshold: float = 0.45) -> list[str]:
        """Flag signals that are heavily weighted but underperform."""
        flagged = []
        sig_summary = {s["signal"]: s for s in self.signal_summary()}
        for sig, stats in sig_summary.items():
            if (
                stats["n"] >= 5
                and stats["win_rate"] / 100 < accuracy_threshold
            ):
                flagged.append(sig)
        return flagged

    def write_report(self) -> str:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        agent_rows = self.agent_summary()
        sig_rows = self.signal_summary()
        lines = [
            "# Super Crypto Agent — Attribution Report",
            "",
            f"Trades recorded: {len(self.history)}",
            "",
            "## Agent P&L Attribution",
            "",
            "| Agent | Trades | Avg P&L% | Win Rate | Avg Confidence |",
            "|-------|--------|----------|----------|----------------|",
        ]
        for r in agent_rows[:15]:
            lines.append(
                f"| {r['agent']} | {r['n_trades']} | {r['avg_pnl_pct']:.1f}% | "
                f"{r['win_rate']:.1f}% | {r['avg_confidence']:.2f} |"
            )
        lines += [
            "",
            "## Signal P&L Attribution",
            "",
            "| Signal | Trades | Avg P&L% | Win Rate |",
            "|--------|--------|----------|----------|",
        ]
        for r in sig_rows[:15]:
            lines.append(
                f"| {r['signal']} | {r['n']} | {r['avg_pnl_pct']:.1f}% | {r['win_rate']:.1f}% |"
            )
        with open(ATTRIB_REPORT, "w") as f:
            f.write("\n".join(lines))
        return ATTRIB_REPORT
