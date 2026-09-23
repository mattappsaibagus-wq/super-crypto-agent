#!/usr/bin/env python3
"""Super Crypto Agent orchestrator.

Pipeline: macro → sentiment → pattern → correlation → discovery → DD → on-chain
→ whale(on-chain) → news → meta-learner → advisor → risk → paper trading.

Run: python3 run_pipeline.py [--loop N] [--coin SYMBOL]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import warnings
from collections import Counter

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports.*")

sys.path.insert(0, os.path.dirname(__file__))

from supercrypto.agents.advisor import InvestmentAdvisor
from supercrypto.agents.correlation import CorrelationAgent
from supercrypto.agents.dd import DueDiligence
from supercrypto.agents.macro import MacroRegime
from supercrypto.agents.meta_learner import MetaLearner
from supercrypto.agents.microcap import MicroCapFinder
from supercrypto.agents.news import NewsScanner
from supercrypto.agents.onchain import OnChainHolder
from supercrypto.agents.pattern import PatternAgent
from supercrypto.agents.sentiment import SentimentAgent
from supercrypto.agents.whale import WhaleDetector
from supercrypto.config import COINGECKO_BASE, DATA_DIR, SIGNALS_FILE, WATCHLIST_FILE
from supercrypto.core.attribution import AttributionEngine
from supercrypto.core.base import api_get, coin_id_for, ensure_dirs
from supercrypto.core.paper import PaperTrader
from supercrypto.core.risk import RiskManager


def clear_bus():
    ensure_dirs()
    with open(SIGNALS_FILE, "w") as f:
        json.dump([], f)


def load_prices(verdicts):
    ids, prices = {}, {}
    for v in verdicts:
        cid = coin_id_for(v["coin"])
        if cid:
            ids[cid] = v["coin"]
    if ids:
        data = (
            api_get(
                f"{COINGECKO_BASE}/simple/price",
                params={"ids": ",".join(ids), "vs_currencies": "usd"},
            )
            or {}
        )
        for cid, sym in ids.items():
            if cid in data and isinstance(data[cid], dict):
                prices[sym] = data[cid].get("usd")
    return prices


def run_once(args) -> int:
    if not getattr(args, "no_clear", True) and not args.quick:
        clear_bus()
    else:
        ensure_dirs()

    print("super-crypto-agent — multi-agent signal & research engine (paper-trading only)")
    t0 = time.time()

    print("[0] macro regime")
    MacroRegime().execute()
    regime = MacroRegime.current_regime()
    print("    regime:", regime)

    quick = getattr(args, "quick", True)
    if not quick:
        print("[1-a] sentiment analysis")
        SentimentAgent().execute()
        time.sleep(0.5)

        print("[1-b] pattern recognition")
        PatternAgent().execute()
        time.sleep(0.5)

        print("[1-c] correlation monitoring")
        CorrelationAgent().execute(coins=["BTC", "ETH", "SOL", "ADA", "AVAX"])

    print("[1-d] discovery")
    MicroCapFinder().execute()
    WhaleDetector().execute()
    NewsScanner().execute(coins=[args.coin.upper()] if args.coin else None)
    print("    done in %.1fs" % (time.time() - t0))

    print("[2] due diligence")
    DueDiligence().execute()

    # On-chain agents need API keys — skip entirely if absent
    if os.environ.get("ETHERSCAN_API_KEY"):
        print("[2b] on-chain holders")
        OnChainHolder().execute()
        print("[2c] whale on-chain transfers")
        WhaleDetector()._onchain_transfers()

    print("[3] meta-learner")
    MetaLearner().execute()

    print("[4] advisor")
    verdicts = InvestmentAdvisor().execute(regime=regime)

    print("[5] risk management")
    risk_cfg = {}
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            risk_cfg = json.load(f)
    paper = PaperTrader()
    verdicts = RiskManager(risk_cfg).adjust(verdicts, paper)

    print("[6] paper trading")
    before_positions = dict(paper.state.get("positions", {}))
    paper.process(verdicts, load_prices(verdicts))
    after_positions = paper.state.get("positions", {})

    # Attribution: record closed trades
    attrib = AttributionEngine()
    for coin in list(before_positions.keys()):
        if coin not in after_positions:
            pos = before_positions[coin]
            exit_price = pos.get("last_price", pos.get("entry_price", 0))
            # Find contributing signals from the bus
            try:
                with open(SIGNALS_FILE) as f:
                    bus = json.load(f)
            except (OSError, json.JSONDecodeError):
                bus = []
            contributors = [
                {
                    "signal": s.get("signal", ""),
                    "agent": s.get("agent", ""),
                    "confidence": s.get("confidence", 0),
                    "weight": 0.5,
                }
                for s in bus
                if s.get("coin") == coin
                and s.get("signal")
                in (
                    "whale_up",
                    "news_bullish",
                    "sentiment_shot",
                    "pattern_bullish",
                    "correlation_spike",
                )
            ]
            attrib.record_trade(
                coin, pos["entry_price"], exit_price, "closed_in_cycle", contributors
            )

    m = paper.metrics()
    print(
        "    equity %.1f | open %d | closed %d | win %s%% | maxDD %s%%"
        % (
            m["current_equity"],
            m["open_positions"],
            m["closed_trades"],
            m["win_rate"],
            m["max_drawdown_pct"],
        )
    )

    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE) as f:
            bus = json.load(f)
        counts = Counter(s.get("agent", "?") for s in bus)
        print("bus: %d signals - %s" % (len(bus), dict(counts)))

    print("top verdicts:")
    for v in verdicts[:8]:
        mark = {"BUY": "🟢", "HOLD": "🔵", "WATCH": "🟡", "AVOID": "🔴"}.get(
            v["action"], "⚪"
        )
        risk = (
            " [risk: %s]" % "; ".join(v.get("risk_notes", []))
            if v.get("risk_notes")
            else ""
        )
        dd = v["dd"] if v["dd"] is not None else "-"
        print(
            "  %s %-10s %-6s score=%+.3f dd=%s%s"
            % (mark, v["coin"], v["action"], v["score"], dd, risk)
        )
    print("done in %.1fs - reports in data/reports/" % (time.time() - t0))
    return 0


def main():
    ap = argparse.ArgumentParser(description="super-crypto-agent multi-agent pipeline")
    ap.add_argument(
        "--loop",
        type=float,
        default=0,
        help="Re-run every N hours (e.g. --loop 6). Never wipes the bus.",
    )
    ap.add_argument("--coin", help="Extra coin symbol for news focus")
    ap.add_argument("--no-clear", action="store_true", help="Keep prior signals")
    ap.add_argument(
        "--quick", action="store_true", default=True,
        help="Skip slow agents (sentiment, pattern, correlation) for cloud/faster runs",
    )
    args = ap.parse_args()

    if args.loop:
        args.no_clear = True
        while True:
            print(
                "=== cycle %s ==="
                % datetime.datetime.now().isoformat(timespec="seconds")
            )
            run_once(args)
            print("sleeping %.1fh..." % args.loop)
            time.sleep(args.loop * 3600)
    else:
        sys.exit(run_once(args))


if __name__ == "__main__":
    main()
