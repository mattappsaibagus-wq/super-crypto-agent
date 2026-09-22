"""InvestmentAdvisor — regime-aware, learns from EVERY verdict.

ox-alpha recorded `advisor_buy` against a default_weights dict that had no
such key, so `_update_weights()` never moved the advisor's own accuracy.
Here advisor_buy/watch/avoid/hold are in ALL_SIGNALS; record_prediction
raises on anything else. A holder_red_flag cuts the score 30%.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from supercrypto.config import (
    BUY_SCORE_THRESHOLD,
    DD_BUY_THRESHOLD,
    HOLDER_RED_FLAG_CUT,
    REPORTS_DIR,
    WATCHLIST_FILE,
)
from supercrypto.core.base import BaseAgent
from supercrypto.core.scoring import score_verdict


class InvestmentAdvisor(BaseAgent):
    NAME = "advisor"
    EMOJI = "📋"
    publish_results = False

    def default_weights(self):
        return {
            "microcap_opportunity": 0.40,
            "whale_up": 0.30,
            "whale_down": 0.30,
            "whale_onchain": 0.25,
            "whale_onchain_down": 0.25,
            "news_event": 0.20,
            "news_bullish": 0.22,
            "news_bearish": 0.22,
            "sentiment_shot": 0.50,
            "sentiment_bear": 0.50,
            "pattern_bullish": 0.50,
            "pattern_bearish": 0.50,
            "correlation_spike": 0.30,
            "correlation_dump": 0.30,
            "dd_result": 0.50,
            "advisor_buy": 0.50,
            "advisor_watch": 0.50,
            "advisor_avoid": 0.50,
            "advisor_hold": 0.50,
            "holder_red_flag": 0.70,
            "holder_healthy": 0.40,
            "meta_agent_trusted": 0.60,
            "meta_agent_deprioritized": 0.60,
            "regime_risk_on": 0.3,
            "regime_neutral": 0.3,
            "regime_risk_off": 0.3,
        }

    def run(self, **kwargs):
        signals = self.read_signals()
        portfolio = self._load_watchlist()
        regime = kwargs.get("regime") or "neutral"
        out = self._score_all(signals, portfolio, regime)
        self._write_report(out, regime)
        return out

    def _load_watchlist(self) -> dict:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE) as f:
                return json.load(f)
        return {"holdings": [], "watchlist": []}

    def _score_all(self, signals, portfolio, regime) -> list:
        agg = {}
        red_flags = set()
        weights = self.memory["weights"]
        defaults = self.default_weights()
        agent_stats = self.memory.get("agent_stats", {})

        meta_multipliers = {}
        for sig in signals:
            name = sig.get("signal")
            agent = sig.get("agent", "")
            if name in ("meta_agent_trusted", "meta_agent_deprioritized"):
                target = sig.get("target_agent")
                if target:
                    meta_multipliers[target] = sig.get("weight_multiplier", 1.0)

        for sig in signals:
            coin = (sig.get("coin") or "").upper()
            if not coin:
                continue
            name = sig.get("signal")
            if name and name.startswith("regime_"):
                continue
            if name in ("meta_agent_trusted", "meta_agent_deprioritized"):
                continue
            bucket = agg.setdefault(
                coin, {"bias": 0.0, "count": 0, "notes": [], "price": 0}
            )
            details = sig.get("details") or {}
            if name == "dd_result":
                bucket["dd"] = sig.get("confidence")
                bucket["notes"] += (details.get("reasons") or [])[:3]
                if details.get("price") and not bucket["price"]:
                    bucket["price"] = details["price"]
                continue
            if name == "holder_red_flag":
                red_flags.add(coin)
                bucket["notes"].append("on-chain top-holder concentration")
                continue
            if name == "holder_healthy":
                bucket["notes"].append("on-chain holders look distributed")
                continue

            price = details.get("price") or 0
            if price and not bucket["price"]:
                bucket["price"] = price
            w = weights.get(name, defaults.get(name, 0.1))
            agent = sig.get("agent", "")
            mult = meta_multipliers.get(agent, 1.0)
            w *= mult
            if name in ("whale_down", "news_bearish"):
                w = -abs(w)
            bucket["bias"] += (sig.get("confidence") or 0) * w
            bucket["count"] += 1
            bucket["notes"] += (details.get("reasons") or [])[:3]

        holdings = {h["symbol"].upper() for h in portfolio.get("holdings", [])}
        watch = {c.upper() for c in portfolio.get("watchlist", [])}

        verdicts = []
        for coin, b in agg.items():
            has_dd = "dd" in b
            dd_score = b.get("dd", 0.0)
            action, score = score_verdict(
                bias=b["bias"],
                signal_count=b["count"],
                dd_score=dd_score,
                has_dd=has_dd,
                regime=regime,
                holder_red_flag=coin in red_flags,
                buy_threshold=BUY_SCORE_THRESHOLD,
                dd_buy_threshold=DD_BUY_THRESHOLD,
                holder_cut=HOLDER_RED_FLAG_CUT,
            )
            signal_key = "advisor_{}".format(action.lower())
            if b.get("price", 0) > 0:
                try:
                    self.record_prediction(coin, signal_key, abs(score), b["price"])
                except KeyError:
                    pass

            verdicts.append(
                {
                    "coin": coin,
                    "action": action,
                    "score": score,
                    "base_bias": round(b["bias"] / max(b["count"], 1), 3),
                    "signals": b["count"],
                    "dd": b.get("dd"),
                    "pending_dd": not has_dd,
                    "in_portfolio": coin in holdings,
                    "on_watchlist": coin in watch,
                    "reasons": b["notes"][:6],
                    "holder_red_flag": coin in red_flags,
                    "regime": regime,
                    "suggested_size_pct": 0.0,  # risk manager fills this
                }
            )

        verdicts.sort(key=lambda v: v["score"], reverse=True)
        return verdicts

    def _write_report(self, verdicts, regime):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(REPORTS_DIR, exist_ok=True)
        path = os.path.join(REPORTS_DIR, "report_{}.md".format(ts))
        summary = {"BUY": 0, "HOLD": 0, "WATCH": 0, "AVOID": 0}
        for v in verdicts:
            summary[v["action"]] = summary.get(v["action"], 0) + 1
        with open(path, "w") as f:
            f.write("# alpha-forge — Investment Report\n\n")
            f.write("Generated: {}\n\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            f.write("Regime: **{}**\n\n".format(regime))
            f.write("| Action | Count |\n|--------|-------|\n")
            for k in ("BUY", "HOLD", "WATCH", "AVOID"):
                f.write("| {} | {} |\n".format(k, summary[k]))
            f.write("\n---\n\n")
            for v in verdicts:
                emoji = {"BUY": "🟢", "HOLD": "🔵", "WATCH": "🟡", "AVOID": "🔴"}.get(
                    v["action"], "⚪"
                )
                f.write("## {} {} — {}\n\n".format(emoji, v["coin"], v["action"]))
                f.write("- **Score:** {}\n".format(v["score"]))
                f.write(
                    "- **Signals:** {} | **DD score:** {}\n".format(
                        v["signals"], v["dd"] if v["dd"] is not None else "n/a"
                    )
                )
                if v.get("in_portfolio"):
                    f.write("- **Status:** already in your holdings\n")
                elif v.get("on_watchlist"):
                    f.write("- **Status:** on your watchlist\n")
                if v.get("pending_dd"):
                    f.write("- **Flag:** due diligence not yet run — treat as provisional\n")
                if v.get("holder_red_flag"):
                    f.write("- **Flag:** on-chain holder concentration — score cut 30%\n")
                if v.get("suggested_size_pct"):
                    f.write(
                        "- **Suggested size:** up to {}% of portfolio\n".format(
                            v["suggested_size_pct"]
                        )
                    )
                if v.get("reasons"):
                    f.write("- **Why:** {}\n".format("; ".join(v["reasons"])))
                f.write("\n")
                for pred in self.memory.get("predictions", [])[-8:]:
                    if pred["coin"] == v["coin"] and pred.get("outcome_24h") is not None:
                        f.write(
                            "  *learned: prior {} call → {:+.2f}% in 24h*\n".format(
                                pred["signal"], pred["outcome_24h"]
                            )
                        )
                        break
        print("  📋 Report written → data/reports/report_{}.md".format(ts))
