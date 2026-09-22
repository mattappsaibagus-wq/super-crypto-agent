#!/usr/bin/env python3
"""Backtest alpha-forge scoring on free Binance daily klines (no API key)."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from supercrypto.config import BINANCE_KLINES
from supercrypto.core.backtest import Backtester
from supercrypto.core.base import api_get


def fetch_klines(symbol: str, days: int):
    data = (
        api_get(
            BINANCE_KLINES,
            params={
                "symbol": symbol.upper(),
                "interval": "1d",
                "limit": min(1000, days),
            },
        )
        or []
    )
    if not isinstance(data, list):
        return []
    return [
        {
            "t": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in data
    ]


def main():
    ap = argparse.ArgumentParser(description="Backtest alpha-forge scoring")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument(
        "--regime",
        default="neutral",
        choices=["risk_on", "neutral", "risk_off"],
    )
    ap.add_argument("--dd-threshold", type=float, default=0.4)
    args = ap.parse_args()

    print("fetching %d daily candles for %s..." % (args.days, args.symbol))
    candles = fetch_klines(args.symbol, args.days)
    if len(candles) < 30:
        print("only got %d candles - check symbol/network" % len(candles))
        return 1
    res = Backtester(
        candles, regime=args.regime, dd_threshold=args.dd_threshold
    ).run()
    print(json.dumps(res, indent=2))
    n = res["trades"]
    if n and res["win_rate"] >= 55 and res["expectancy_pct"] > 2:
        verdict = "PROMISING"
    elif n and res["expectancy_pct"] > 0:
        verdict = "MARGINAL"
    else:
        verdict = "NEGATIVE - do not trust live learning with this config yet"
    print("")
    print("verdict:", verdict)
    print("(single-coin backtest is a smoke test, not proof - run the")
    print(" watchlist, then paper-trade for weeks before real capital.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
