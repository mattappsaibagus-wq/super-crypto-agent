"""Replay scoring.py over OHLCV candles with stop-loss / take-profit simulation.

Uses the same score_whale / score_dd / score_verdict functions as live agents
so a promising backtest is evidence about THIS scoring logic, not a cousin.
"""

from __future__ import annotations

from typing import Optional

from supercrypto.config import (
    BUY_SCORE_THRESHOLD,
    DD_BUY_THRESHOLD,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
)
from supercrypto.core.scoring import regime_multiplier, score_dd, score_verdict, score_whale


def _sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


class Backtester:
    def __init__(
        self,
        candles: list,
        regime: str = "neutral",
        dd_threshold: float = DD_BUY_THRESHOLD,
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
        vol_lookback: int = 20,
    ):
        self.candles = candles
        self.regime = regime
        self.dd_threshold = dd_threshold
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.vol_lookback = vol_lookback

    def _dd_from_window(self, window: list) -> float:
        """Synthetic DD from price/volume path — no CoinGecko in the backtest."""
        closes = [c["close"] for c in window]
        volumes = [c["volume"] for c in window]
        last = window[-1]
        peak = max(closes) if closes else last["close"]
        ath_change = (last["close"] / peak - 1.0) * 100 if peak else 0.0
        # Rough mcap/vol proxies: use price * volume as a stand-in so the
        # relative ratios in score_dd still move.
        vol = last["volume"]
        mcap_proxy = last["close"] * vol * 50  # arbitrary but stable scale
        change_7d = 0.0
        change_30d = 0.0
        if len(closes) >= 8 and closes[-8] > 0:
            change_7d = (closes[-1] / closes[-8] - 1.0) * 100
        if len(closes) >= 31 and closes[-31] > 0:
            change_30d = (closes[-1] / closes[-31] - 1.0) * 100
        info = {
            "mcap": mcap_proxy,
            "vol": vol,
            "price": last["close"],
            "ath_change_pct": ath_change,
            "rag_status": "indexed",
            "pair_count": 4,
            "top_pair_liq_share": 0.5,
            "change_7d": change_7d,
            "change_30d": change_30d,
            "rank": 800,
        }
        score, _ = score_dd(info)
        return score

    def run(self) -> dict:
        candles = self.candles
        n = len(candles)
        trades = []
        position = None  # {entry_idx, entry, sl, tp}
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        lookback = max(self.vol_lookback, 30)

        for i in range(lookback, n):
            window = candles[: i + 1]
            last = candles[i]
            close = last["close"]
            high = last["high"]
            low = last["low"]
            volumes = [c["volume"] for c in candles[i - self.vol_lookback : i]]
            avg_vol = _sma(volumes, self.vol_lookback) or last["volume"]
            vol_ratio = (last["volume"] / avg_vol) if avg_vol else 0.0
            prev = candles[i - 1]["close"]
            change_pct = (close / prev - 1.0) * 100 if prev else 0.0

            # Manage open trade first (intrabar high/low vs SL/TP).
            if position is not None:
                entry = position["entry"]
                sl_px = entry * (1.0 - self.stop_loss_pct / 100.0)
                tp_px = entry * (1.0 + self.take_profit_pct / 100.0)
                exit_px = None
                reason = None
                if low <= sl_px:
                    exit_px = sl_px
                    reason = "stop_loss"
                elif high >= tp_px:
                    exit_px = tp_px
                    reason = "take_profit"
                if exit_px is not None:
                    pnl = (exit_px / entry - 1.0) * 100
                    equity *= exit_px / entry
                    trades.append(
                        {
                            "entry_idx": position["entry_idx"],
                            "exit_idx": i,
                            "pnl_pct": round(pnl, 3),
                            "reason": reason,
                        }
                    )
                    position = None

            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak else 0.0
            if dd > max_dd:
                max_dd = dd

            if position is not None:
                continue

            whale_sig, whale_conf, _ = score_whale(change_pct, vol_ratio, last["volume"])
            dd_score = self._dd_from_window(window)
            bias = 0.0
            count = 0
            if whale_sig == "whale_up":
                bias += whale_conf * 0.30
                count += 1
            elif whale_sig == "whale_down":
                bias -= whale_conf * 0.30
                count += 1
            # Microcap-style volume/mcap stand-in: treat high vol_ratio as opportunity.
            if vol_ratio > 2 and change_pct > 0:
                bias += min(1.0, vol_ratio / 5) * 0.40
                count += 1

            action, score = score_verdict(
                bias=bias,
                signal_count=max(count, 1),
                dd_score=dd_score,
                has_dd=True,
                regime=self.regime,
                buy_threshold=BUY_SCORE_THRESHOLD,
                dd_buy_threshold=self.dd_threshold,
            )
            if action == "BUY":
                position = {"entry_idx": i, "entry": close}

        # Flatten leftover position at last close.
        if position is not None:
            last_close = candles[-1]["close"]
            pnl = (last_close / position["entry"] - 1.0) * 100
            equity *= last_close / position["entry"]
            trades.append(
                {
                    "entry_idx": position["entry_idx"],
                    "exit_idx": n - 1,
                    "pnl_pct": round(pnl, 3),
                    "reason": "eod",
                }
            )
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak else 0.0
            if dd > max_dd:
                max_dd = dd

        n_trades = len(trades)
        wins = [t for t in trades if t["pnl_pct"] > 0]
        expectancy = (
            sum(t["pnl_pct"] for t in trades) / n_trades if n_trades else 0.0
        )
        return {
            "candles": n,
            "trades": n_trades,
            "win_rate": round(len(wins) / n_trades * 100, 2) if n_trades else 0.0,
            "expectancy_pct": round(expectancy, 3),
            "total_return_pct": round((equity - 1.0) * 100, 3),
            "max_drawdown_pct": round(max_dd, 3),
            "regime": self.regime,
            "regime_multiplier": regime_multiplier(self.regime),
        }
