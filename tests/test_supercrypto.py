"""Offline tests for the enhanced Super Crypto Agent (no network)."""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supercrypto.config import ALL_SIGNALS
from supercrypto.core.attribution import AttributionEngine
from supercrypto.core.learning import WeightLearner


# ── New signal types in registry ───────────────────────────────────────────

def test_new_signals_registered():
    for key in ("sentiment_shot", "sentiment_bear", "pattern_bullish", "pattern_bearish",
                "correlation_spike", "correlation_dump", "meta_agent_trusted",
                "meta_agent_deprioritized", "whale_onchain", "whale_onchain_down"):
        assert key in ALL_SIGNALS, f"{key} missing from ALL_SIGNALS"


# ── Enhanced WeightLearner ─────────────────────────────────────────────────

def test_regime_weights_isolated():
    mem = {"weights": {}}
    learner = WeightLearner(mem, {"whale_up"}, min_samples=1)
    learner.update_weights([
        {"signal": "whale_up", "confidence": 0.7, "outcome_24h": 25.0, "outcome_7d": None,
         "regime": "risk_on"},
        {"signal": "whale_up", "confidence": 0.7, "outcome_24h": -5.0, "outcome_7d": None,
         "regime": "risk_off"},
    ])
    # Risk-on should trend higher, risk-off lower
    assert learner.regime_weights["risk_on"]["whale_up"] > learner.regime_weights["risk_off"]["whale_up"]


def test_agent_weight_adjustment_boost():
    mem = {"weights": {}, "agent_stats": {}}
    learner = WeightLearner(mem, {"whale_up"}, min_samples=5)
    learner.memory["agent_stats"]["whale"] = {
        "samples": 20, "wins": 15, "win_rate": 0.75, "avg_return": 3.0,
    }
    adj = learner.agent_weight_adjustment("whale")
    assert 1.0 < adj <= 1.5  # trusted agent gets a boost


def test_cross_agent_share_boosts_signal():
    mem = {"weights": {"sentiment_shot": 0.5}, "agent_stats": {}}
    learner = WeightLearner(mem, {"sentiment_shot"}, min_samples=1)
    learner.cross_agent_share("pattern", ["sentiment_shot", "pattern_bullish"], top_n=1)
    assert mem["weights"]["sentiment_shot"] > 0.5


# ── Attribution engine ─────────────────────────────────────────────────────

def test_attribution_records_trade():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "attrib.json")
    attr = AttributionEngine(path=path)
    attr.record_trade("ETH", 100.0, 120.0, "take_profit", [
        {"signal": "sentiment_shot", "agent": "sentiment", "confidence": 0.7, "weight": 0.5},
    ])
    assert len(attr.history) == 1
    assert attr.history[0]["pnl_pct"] == 20.0
    summary = attr.agent_summary()
    assert summary[0]["agent"] == "sentiment"
    assert summary[0]["n_trades"] == 1


def test_attribution_overfit_detection():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "attrib.json")
    attr = AttributionEngine(path=path)
    for _ in range(10):
        attr.record_trade("X", 100.0, 90.0, "stop_loss", [
            {"signal": "pattern_bullish", "agent": "pattern", "confidence": 0.8, "weight": 0.5},
        ])
    flagged = attr.overfit_signals()
    assert "pattern_bullish" in flagged


# ── Pattern agent detection (pure logic) ───────────────────────────────────

def test_detect_double_bottom():
    from supercrypto.agents.pattern import detect_double_bottom
    # At least 15 candles with W-shape: 100 → 50 → 80 → 45 → 75 → 105
    closes = [100, 95, 90, 85, 80, 70, 75, 80, 85, 70, 75, 85, 95, 100, 105]
    name, reasons, conf = detect_double_bottom(closes)
    assert name == "pattern_bullish"
    assert conf > 0.5


def test_detect_volume_breakout_bullish():
    from supercrypto.agents.pattern import detect_volume_breakout
    closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 90.5, 91.0, 92.0, 94.0, 98.0]
    volumes = [1000] * 15 + [500000]
    name, conf, reasons = detect_volume_breakout(volumes, closes)
    assert name == "pattern_bullish"
    assert conf > 0.5


# ── Correlation helper ───────────────────────────────────────────────────────

def test_pearson_positive():
    from supercrypto.agents.correlation import _pearson
    x = list(range(20))
    y = [v * 2 for v in x]
    r = _pearson(x, y)
    assert r is not None and r > 0.99


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e)
    print("%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
