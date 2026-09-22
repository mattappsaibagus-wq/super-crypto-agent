#!/usr/bin/env python3
"""Backtest super-crypto-agent scoring across a whole watchlist of Binance pairs."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from run_backtest import fetch_klines
from supercrypto.core.backtest import Backtester


DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--dd-threshold", type=float, default=0.4)
    args = ap.parse_args()

    all_trades = []
    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        candles = fetch_klines(sym, args.days)
        if len(candles) < 30:
            print("%-10s skipped (%d candles)" % (sym, len(candles)))
            continue
        res = Backtester(candles, dd_threshold=args.dd_threshold).run()
        print(
            "%-10s trades=%-4d win=%5.1f%% expectancy=%+.2f%% maxDD=%.1f%%"
            % (
                sym,
                res["trades"],
                res["win_rate"],
                res["expectancy_pct"],
                res["max_drawdown_pct"],
            )
        )
        all_trades.append(res)

    if not all_trades:
        print("no data - check network/symbols")
        return 1

    trades = sum(r["trades"] for r in all_trades)
    wins = sum(r["trades"] * r["win_rate"] / 100 for r in all_trades)
    exp = sum(r["expectancy_pct"] * r["trades"] for r in all_trades) / max(trades, 1)
    print(
        "\nPORTFOLIO: %d trades | win %.1f%% | expectancy %+.2f%%"
        % (trades, wins / max(trades, 1) * 100, exp)
    )
    if exp > 2:
        verdict = "PROMISING"
    elif exp > 0:
        verdict = "MARGINAL"
    else:
        verdict = "NEGATIVE - do not trust live learning yet"
    print("verdict:", verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
