"""Offline tests: `python3 tests/test_supercrypto.py` (no network)."""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supercrypto.config import ALL_SIGNALS, KNOWN_IDS
from supercrypto.core.learning import WeightLearner, magnitude_reward
from supercrypto.core.scoring import (
    classify_regime,
    holder_concentration_score,
    market_traction_score,
    regime_multiplier,
    score_dd,
    score_verdict,
    score_whale,
)
from supercrypto.core.backtest import Backtester
from supercrypto.core.paper import PaperTrader
from supercrypto.core.risk import RiskManager
from supercrypto.agents.news import classify_headline


# --- bug fix 1: DD weights are real ---

def test_dd_computes_all_five_weights():
    info = {
        "mcap": 10e6,
        "vol": 2e6,
        "price": 0.5,
        "ath_change_pct": -20,
        "rag_status": "indexed",
        "pair_count": 4,
        "top_pair_liq_share": 0.5,
        "change_7d": 15.0,
        "change_30d": 30.0,
        "rank": 800,
    }
    score, reasons = score_dd(info)
    assert 0 < score <= 1.0
    joined = " ".join(reasons)
    assert "liquidity spread" in joined  # holder_concentration computed
    assert "7d" in joined  # market_traction computed
    assert "holder_concentration" in joined
    assert "market_traction" in joined


def test_single_pair_concentration_flagged():
    s, note = holder_concentration_score(1, 0.98, 5e6, 1e5)
    assert s < 0.3 and "concentration risk" in note


def test_fading_traction_penalized():
    s, note = market_traction_score(-30.0, -50.0, 5000)
    assert s <= 0.2


# --- bug fix 2: registry guarantees learnable keys ---

def test_registry_creates_weight_keys():
    mem = {"weights": {}}
    WeightLearner(mem, {"advisor_buy", "advisor_watch"})
    assert mem["weights"]["advisor_buy"] == 0.5
    assert mem["weights"]["advisor_watch"] == 0.5


def test_unregistered_signal_raises():
    mem = {"weights": {}}
    learner = WeightLearner(mem, {"advisor_buy"})
    try:
        learner.ensure_registered("advisor_buy")
    except KeyError:
        raise AssertionError("registered key must not raise")
    raised = False
    try:
        learner.ensure_registered("not_a_real_signal")
    except KeyError:
        raised = True
    assert raised, "unregistered signal must raise"


def test_advisor_keys_are_in_global_registry():
    for key in ("advisor_buy", "advisor_watch", "advisor_avoid", "advisor_hold"):
        assert key in ALL_SIGNALS


def test_magnitude_reward_monotonic_bounded():
    assert abs(magnitude_reward(0)) < 1e-9
    assert magnitude_reward(3) < magnitude_reward(10) < magnitude_reward(50)
    assert -1 < magnitude_reward(-50) < 0 and magnitude_reward(50) < 1


def test_weights_move_toward_rewards_and_clamp():
    mem = {"weights": {"whale_up": 0.5}}
    learner = WeightLearner(mem, {"whale_up"}, ema_alpha=0.5, min_samples=1)
    settled = [
        {
            "signal": "whale_up",
            "confidence": 0.7,
            "outcome_24h": 25.0,
            "outcome_7d": None,
        }
    ]
    learner.update_weights(settled)
    assert mem["weights"]["whale_up"] > 0.5
    for _ in range(50):
        learner.update_weights(settled * 30)
    assert mem["weights"]["whale_up"] <= 1.0


def test_blended_outcome_multi_horizon():
    pred = {"outcome_24h": 10.0, "outcome_7d": -4.0}
    blend = WeightLearner.blended_outcome(pred, {"24h": 0.6, "7d": 0.4})
    assert abs(blend - 4.4) < 1e-6


def test_blended_outcome_renormalizes_missing_horizon():
    pred = {"outcome_24h": 10.0, "outcome_7d": None}
    blend = WeightLearner.blended_outcome(pred, {"24h": 0.6, "7d": 0.4})
    assert abs(blend - 10.0) < 1e-6


def test_calibration_ece_and_tax():
    mem = {"weights": {}}
    learner = WeightLearner(mem, {"advisor_buy"}, min_samples=1)
    settled = [
        {"signal": "advisor_buy", "confidence": 0.9, "outcome_24h": -5.0, "outcome_7d": None}
        for _ in range(10)
    ]
    ece = learner.update_calibration(settled)
    assert ece > 0
    tax = learner.confidence_tax()
    assert tax > 0
    taxed = learner.taxed_confidence(0.8)
    assert taxed < 0.8


# --- coin id never uses symbol.lower() as the id ---

def test_known_ids_map_btc_to_bitcoin():
    assert KNOWN_IDS["BTC"] == "bitcoin"
    assert KNOWN_IDS["ETH"] == "ethereum"
    assert KNOWN_IDS["SOL"] == "solana"


# --- news: negation + source credibility ---

def test_news_negation_flips_bullish_phrase():
    score, name, reasons = classify_headline("BTC fails to surge after ETF news", "")
    joined = " ".join(reasons)
    assert "negated-bullish" in joined or name != "news_bullish"


def test_news_source_credibility_weights_reuters_above_unknown():
    strong, _, _ = classify_headline(
        "Ethereum rallies to record high", "https://www.reuters.com/markets/eth"
    )
    weak, _, _ = classify_headline(
        "Ethereum rallies to record high", "https://shillblog.example/eth"
    )
    assert strong > weak


# --- regime ---

def test_regime_classification_and_multiplier():
    assert classify_regime(75, 48.0) == "risk_on"
    assert classify_regime(20, 50.0) == "risk_off"
    assert classify_regime(50, 52.0) == "neutral"
    assert regime_multiplier("risk_off") < regime_multiplier("neutral") < regime_multiplier("risk_on")


def test_holder_red_flag_cuts_score():
    action_ok, score_ok = score_verdict(0.5, 1, 0.8, True, regime="neutral", holder_red_flag=False)
    action_cut, score_cut = score_verdict(0.5, 1, 0.8, True, regime="neutral", holder_red_flag=True)
    assert score_cut < score_ok
    assert abs(score_cut - score_ok * 0.70) < 0.02


def test_whale_emits_on_spike():
    name, conf, _ = score_whale(20.0, 4.0, 1_000_000)
    assert name == "whale_up" and conf >= 0.5
    name, conf, _ = score_whale(-20.0, 4.0, 1_000_000)
    assert name == "whale_down"


# --- risk manager ---

def _paper(tmp):
    return PaperTrader(path=os.path.join(tmp, "paper.json"))


def test_circuit_breaker_blocks_buys():
    tmp = tempfile.mkdtemp()
    paper = _paper(tmp)
    paper.state["peak_equity"] = 100.0
    paper.state["equity"] = 74.0  # -26% > 25% breaker
    rm = RiskManager({"circuit_breaker_drawdown_pct": 25.0})
    out = rm.adjust([{"coin": "PEPE", "action": "BUY", "score": 0.3, "dd": 0.6}], paper)
    assert out[0]["action"] == "WATCH"
    assert any("CIRCUIT" in n for n in out[0]["risk_notes"])


def test_dd_scales_position_size():
    tmp = tempfile.mkdtemp()
    rm = RiskManager(
        {
            "max_position_pct": 5.0,
            "max_total_exposure_pct": 20.0,
            "max_concurrent_buys": 3,
        }
    )
    strong = rm.adjust(
        [{"coin": "AAA", "action": "BUY", "score": 0.3, "dd": 0.9}], _paper(tmp)
    )[0]
    weak = rm.adjust(
        [{"coin": "AAA", "action": "BUY", "score": 0.3, "dd": 0.4}], _paper(tmp)
    )[0]
    assert 0 < weak["suggested_size_pct"] < strong["suggested_size_pct"] <= 5.0


def test_risk_never_invents_a_buy():
    tmp = tempfile.mkdtemp()
    rm = RiskManager({})
    out = rm.adjust([{"coin": "AAA", "action": "WATCH", "score": 0.9, "dd": 0.9}], _paper(tmp))
    assert out[0]["action"] == "WATCH"


# --- paper trader exits ---

def test_stop_loss_and_take_profit():
    tmp = tempfile.mkdtemp()
    p = _paper(tmp)
    v = [
        {
            "coin": "X",
            "action": "BUY",
            "score": 0.3,
            "dd": 0.8,
            "suggested_size_pct": 2.0,
            "stop_loss_pct": 15.0,
            "take_profit_pct": 45.0,
        }
    ]
    p.process(v, {"X": 100.0})
    assert "X" in p.state["positions"]
    p.process([], {"X": 84.0})  # -16% -> stop
    assert "X" not in p.state["positions"]
    assert p.state["closed"][0]["exit_reason"] == "stop_loss"


def test_take_profit_closes():
    tmp = tempfile.mkdtemp()
    p = _paper(tmp)
    p.process(
        [
            {
                "coin": "Y",
                "action": "BUY",
                "suggested_size_pct": 2.0,
                "stop_loss_pct": 15.0,
                "take_profit_pct": 45.0,
            }
        ],
        {"Y": 10.0},
    )
    p.process([], {"Y": 15.0})  # +50%
    assert "Y" not in p.state["positions"]
    assert p.state["closed"][0]["exit_reason"] == "take_profit"


def test_paper_path_none_stays_in_memory():
    p = PaperTrader(path=None)
    p.process(
        [{"coin": "Z", "action": "BUY", "suggested_size_pct": 5.0, "stop_loss_pct": 15, "take_profit_pct": 45}],
        {"Z": 1.0},
    )
    assert "Z" in p.state["positions"]
    assert not os.path.exists("paper.json")


# --- backtest engine runs on synthetic data ---

def test_backtest_synthetic():
    import random

    random.seed(7)
    candles = [
        {
            "t": i,
            "open": 100,
            "high": 100 + i % 5,
            "low": 95,
            "close": 100 + (i % 7),
            "volume": 1000 + (5000 if i % 29 == 0 else 0),
        }
        for i in range(120)
    ]
    res = Backtester(candles).run()
    assert res["candles"] == 120
    for k in ("trades", "win_rate", "total_return_pct", "max_drawdown_pct"):
        assert k in res


def test_watchlist_has_risk_block():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "data", "watchlist.json")
    with open(path) as f:
        cfg = json.load(f)
    assert "risk" in cfg
    assert cfg["risk"]["circuit_breaker_drawdown_pct"] == 25.0


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
