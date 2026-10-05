"""Offline tests for the Kronos forecast agent (no network, no torch needed).

The pure decision / grading logic always runs. The end-to-end agent test uses
a fake predictor and synthetic candles; it needs pandas and is skipped
without it (CI installs only requirements.txt).
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supercrypto import config  # noqa: E402
from supercrypto.agents import kronos as K  # noqa: E402
from supercrypto.config import ALL_SIGNALS, BEARISH_SIGNALS  # noqa: E402


def test_signals_registered():
    assert "kronos_forecast_up" in ALL_SIGNALS
    assert "kronos_forecast_down" in ALL_SIGNALS
    assert "kronos_forecast_down" in BEARISH_SIGNALS
    assert "kronos_forecast_up" not in BEARISH_SIGNALS


def test_decide_strong_up():
    d = K.decide([2.1, 2.4, 1.9, 2.2, 2.6, 2.0, 2.3, 1.8], daily_vol_pct=3.0)
    assert d and d["direction"] == 1
    assert 0.45 <= d["conf"] <= 0.85
    assert d["agreement"] == 1.0


def test_decide_strong_down():
    d = K.decide([-3.0, -2.5, -2.8, -3.3, -2.9, -2.6, -3.1, -2.7], daily_vol_pct=4.0)
    assert d and d["direction"] == -1


def test_decide_rejects_noise():
    # mixed directions
    assert K.decide([1.5, -1.2, 2.0, -0.8, 1.1, -1.9, 0.7, 1.3], daily_vol_pct=3.0) is None
    # agreeing but below the noise floor
    assert K.decide([0.3, 0.4, 0.2, 0.35, 0.3, 0.25, 0.4, 0.3], daily_vol_pct=3.0) is None


def test_decide_rejects_implausible_move():
    # +40% in 24h on a coin with 3% daily vol: show it, never signal it
    assert K.decide([40, 41, 39, 40.5, 40, 39.5, 41, 40], daily_vol_pct=3.0) is None


def test_decide_handles_bad_input():
    assert K.decide([]) is None
    assert K.decide([1.0]) is None
    assert K.decide([1.0, float("nan"), 2.0]) is None


def test_percentile():
    assert K.percentile([1, 2, 3, 4, 5], 50) == 3
    assert abs(K.percentile([0, 10], 10) - 1.0) < 1e-9


def test_grade_and_track_record():
    a = K.grade({"coin": "BTC", "reference_price": 100.0, "expected_move_pct": 2.0,
                 "range_pct": [0.5, 3.5], "signal": "kronos_forecast_up"}, 102.5)
    assert a["direction_hit"] and a["in_range"] and a["actual_move_pct"] == 2.5
    b = K.grade({"coin": "ETH", "reference_price": 100.0, "expected_move_pct": 1.5,
                 "range_pct": [0.2, 2.5], "signal": None}, 98.0)
    assert not b["direction_hit"] and not b["in_range"]
    tr = K.track_record([a, b, {"coin": "SOL", "graded": False}])
    assert tr["all"]["n"] == 2 and tr["all"]["hit_rate"] == 50.0
    assert tr["signals"]["n"] == 1 and tr["signals"]["hit_rate"] == 100.0
    assert tr["pending"] == 1
    # following the calls: +2.5 (right) and -2.0 (wrong) -> +0.25 avg
    assert tr["all"]["avg_return"] == 0.25


class _FakeFrame:
    def __init__(self, closes):
        self._c = closes

    def __getitem__(self, key):
        assert key == "close"
        return self

    def tolist(self):
        return list(self._c)


class FakePredictor:
    """Every path for every coin drifts by `drift` per 4h bar (plus tiny noise)."""

    def __init__(self, drift):
        self.drift = drift
        self.calls = 0

    def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list, pred_len, **kw):
        self.calls += 1
        out = []
        rnd = random.Random(self.calls)
        for df in df_list:
            last = float(df["close"].iloc[-1])
            closes, p = [], last
            for _ in range(pred_len):
                p *= 1 + self.drift + rnd.gauss(0, 0.0005)
                closes.append(p)
            out.append(_FakeFrame(closes))
        return out


def _synthetic_candles(sym, ref=None):
    rnd = random.Random(sym)
    step = config.KRONOS_INTERVAL_HOURS * 3600
    now = int(time.time() // step * step)
    p, bars = 100.0, []
    for i in range(config.KRONOS_LOOKBACK):
        o = p
        p *= math.exp(rnd.gauss(0, 0.01))
        bars.append((now - (config.KRONOS_LOOKBACK - i) * step, o, max(o, p) * 1.005,
                     min(o, p) * 0.995, p, 1000.0, 1000.0 * p))
    return bars, p


def test_agent_end_to_end_with_fake_predictor():
    try:
        import pandas  # noqa: F401
    except ImportError:
        print("  (skipped: pandas not installed)")
        return
    tmp = tempfile.mkdtemp()
    orig = (K.KRONOS_FORECASTS_FILE, K.SIGNALS_FILE)
    import supercrypto.core.base as base
    orig_base = (base.SIGNALS_FILE, base.MEMORY_DIR)
    try:
        K.KRONOS_FORECASTS_FILE = os.path.join(tmp, "kronos_forecasts.json")
        K.SIGNALS_FILE = base.SIGNALS_FILE = os.path.join(tmp, "signals.json")
        base.MEMORY_DIR = os.path.join(tmp, "memory")
        os.makedirs(base.MEMORY_DIR, exist_ok=True)
        # a stale Kronos call from an earlier scan must be replaced
        with open(base.SIGNALS_FILE, "w") as f:
            json.dump([{"coin": "BTC", "signal": "kronos_forecast_down", "agent": "kronos",
                        "source": "kronos", "confidence": 0.7}], f)
        base.fetch_markets = K.fetch_markets = lambda per_page=50: []

        agent = K.KronosForecast(predictor=FakePredictor(drift=0.004))  # ~+2.4%/24h
        agent.memory_path = os.path.join(base.MEMORY_DIR, "kronos_memory.json")
        agent._candles = _synthetic_candles
        sigs = agent.execute(coins=["BTC", "ETH"])
        assert {s["coin"] for s in sigs} == {"BTC", "ETH"}, sigs
        assert all(s["signal"] == "kronos_forecast_up" for s in sigs)
        bus = json.load(open(base.SIGNALS_FILE))
        assert not any(b["signal"] == "kronos_forecast_down" for b in bus), "stale call kept"
        store = json.load(open(K.KRONOS_FORECASTS_FILE))
        fc = store["latest"]["BTC"]
        assert len(fc["forecast"]) == config.KRONOS_PRED_LEN and fc["history"]
        assert fc["signal"] == "kronos_forecast_up" and fc["expected_move_pct"] > 1
        assert store["track_record"]["pending"] == 2

        # flat forecast -> forecasts stored, but no signals
        agent2 = K.KronosForecast(predictor=FakePredictor(drift=0.0))
        agent2.memory_path = agent.memory_path
        agent2._candles = _synthetic_candles
        assert agent2.execute(coins=["BTC"]) == []
        assert json.load(open(K.KRONOS_FORECASTS_FILE))["latest"]["BTC"]["signal"] is None
    finally:
        K.KRONOS_FORECASTS_FILE, K.SIGNALS_FILE = orig
        base.SIGNALS_FILE, base.MEMORY_DIR = orig_base


def test_agent_sits_out_without_model():
    os.environ["KRONOS_ENABLED"] = "0"
    try:
        agent = K.KronosForecast()
        assert agent._load() is False
        assert "disabled" in agent.unavailable_reason
    finally:
        del os.environ["KRONOS_ENABLED"]



def test_candles_reject_namesake_token():
    agent = K.KronosForecast(predictor=object())
    bars = [(0, 1, 1, 1, 0.065, 1, 1)] * 3
    agent._binance = staticmethod(lambda sym, limit: bars)
    agent._kucoin = staticmethod(lambda sym, limit: None)
    got, why = agent._candles("BEAM", ref=0.0027)       # Binance BEAM != CoinGecko Beam
    assert got is None and why == "ticker_mismatch"
    got, live = agent._candles("BEAM", ref=0.066)        # same asset: accepted
    assert got is not None

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL", fn.__name__, "-", repr(e))
    print("%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
