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


# ── New data sources (Santiment, Hyperliquid, crypto-fundraising) ──────────

def test_source_signals_registered():
    for key in ("dev_activity_up", "active_addresses_spike", "active_addresses_fade",
                "social_spike", "funding_squeeze", "oi_buildup", "funding_overheated",
                "oi_flush", "fresh_funding"):
        assert key in ALL_SIGNALS, f"{key} missing from ALL_SIGNALS"


def test_santiment_dev_and_address_spikes():
    from supercrypto.agents.santiment import analyse
    dev = [10] * 21 + [40] * 7          # weekly pace 70 → 280 = 4x
    daa = [1000] * 15 + [2600]          # 2.6x the 14d median
    names = {n for n, _, _ in analyse({"dev_activity_1d": dev, "daily_active_addresses": daa})}
    assert names == {"dev_activity_up", "active_addresses_spike"}
    fade = analyse({"daily_active_addresses": [1000] * 15 + [300]})
    assert fade and fade[0][0] == "active_addresses_fade"
    assert analyse({"dev_activity_1d": [5] * 10}) == []   # too little history


# Shape copied from a live api.hyperliquid.xyz metaAndAssetCtxs response.
HL_PAYLOAD = [
    {"universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
        {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10},
        {"name": "DEAD", "szDecimals": 0, "maxLeverage": 3, "isDelisted": True},
    ]},
    [
        {"funding": "0.0000125", "openInterest": "37717.38", "markPx": "87068.0",
         "prevDayPx": "83906.0", "dayNtlVlm": "2946768889.77"},
        {"funding": "-0.00008", "openInterest": "900000000", "markPx": "0.012",
         "prevDayPx": "0.011", "dayNtlVlm": "50000000"},
        {"funding": "0.001", "openInterest": "1", "markPx": "1", "prevDayPx": "1",
         "dayNtlVlm": "1"},
    ],
]


def test_hyperliquid_parse_and_normalise():
    from supercrypto.agents.derivatives import parse_markets
    m = parse_markets(HL_PAYLOAD)
    assert set(m) == {"BTC", "PEPE"}                     # delisted dropped, k-prefix stripped
    assert abs(m["PEPE"]["price"] - 0.000012) < 1e-12    # kPEPE priced per 1,000
    assert m["BTC"]["oi_usd"] > 3e9


def test_hyperliquid_signals():
    from supercrypto.agents.derivatives import evaluate, parse_markets
    m = parse_markets(HL_PAYLOAD)
    names = {n for n, _, _ in evaluate("PEPE", m["PEPE"])}
    assert "funding_squeeze" in names                    # shorts paying, price up
    prev = {"oi_usd": m["BTC"]["oi_usd"] / 1.5, "price": m["BTC"]["price"] / 1.05}
    names = {n for n, _, _ in evaluate("BTC", m["BTC"], prev)}
    assert "oi_buildup" in names
    hot = dict(m["BTC"], funding=0.0003)
    assert evaluate("BTC", hot)[0][0] == "funding_overheated"


# Structure mirrors crypto-fundraising.info /deal-flow/ (div.hpt-data rows).
DEAL_HTML = """
<div class="hp-table dealflow-table">
 <div class="hp-table-row hpt-header"><div>#</div><div>Project</div></div>
 <div class="hp-table-row hpt-data">
  <div class="hpt-col1"> 01</div>
  <div class="hpt-col2"><a class="t-project-link" href="/projects/grass">
    <div class="coininfo"><h5 class="cointitle">Grass</h5><span class="cointag">GRASS</span></div></a></div>
  <div class="hpt-col3">Series A</div>
  <div class="hpt-col3">Sep 2026</div>
  <div class="hpt-col4">Series A <span>Raised</span> $12.5M</div>
  <div class="hpt-col4 centred"> -</div>
  <div class="hpt-col4 centred"> Yes</div>
  <div class="hpt-col5 flexwrap"><a>AI</a> <a>DePIN</a></div>
  <div class="hpt-col6 flexwrap nojustify">Investors: <a>Multicoin Capital</a></div>
 </div>
 <div class="hp-table-row hpt-data">
  <div class="hpt-col1"> 02</div>
  <div class="hpt-col2"><a class="t-project-link" href="/projects/uorm">
    <div class="coininfo"><h5 class="cointitle">Uorm</h5><span class="cointag">UORM</span></div></a></div>
  <div class="hpt-col3">Pre-seed</div>
  <div class="hpt-col3">Oct 2026</div>
  <div class="hpt-col4">Pre-seed Raised $750k</div>
  <div class="hpt-col4 centred"> -</div>
  <div class="hpt-col4 centred"> No</div>
  <div class="hpt-col5 flexwrap">Gaming</div>
  <div class="hpt-col6 flexwrap nojustify">Investors: Marqel Capital</div>
 </div>
</div>
"""


def test_fundraising_parse():
    from supercrypto.agents.fundraising import parse_deals, score_deal
    deals = parse_deals(DEAL_HTML)
    assert [d["project"] for d in deals] == ["Grass", "Uorm"]
    g, u = deals
    assert g["ticker"] == "GRASS" and g["tradable"] and g["raised_usd"] == 12.5e6
    assert "Multicoin" in g["investors"]
    assert u["tradable"] is False and u["raised_usd"] == 750e3
    assert score_deal(g) > score_deal(u)                 # bigger round + top-tier VC


def test_advisor_subtracts_bearish_sources():
    from supercrypto.config import BEARISH_SIGNALS
    for key in ("funding_overheated", "oi_flush", "active_addresses_fade",
                "sentiment_bear", "pattern_bearish", "whale_onchain_down"):
        assert key in BEARISH_SIGNALS



# ── Signal bus freshness ───────────────────────────────────────────────────

def _with_tmp_bus(fn):
    import json as _json
    import supercrypto.core.base as base
    orig = base.SIGNALS_FILE
    path = os.path.join(tempfile.mkdtemp(), "signals.json")
    base.SIGNALS_FILE = path
    try:
        return fn(base, path, _json)
    finally:
        base.SIGNALS_FILE = orig


def test_prune_drops_stale_signals_keeps_fresh_and_dd():
    from datetime import datetime, timedelta, timezone

    def run(base, path, json):
        now = datetime.now(timezone.utc)
        ago = lambda h: (now - timedelta(hours=h)).isoformat()  # noqa: E731
        bus = [
            {"coin": "A", "signal": "microcap_opportunity", "timestamp": ago(2)},
            {"coin": "B", "signal": "microcap_opportunity", "timestamp": ago(30)},
            {"coin": "C", "signal": "dd_result", "timestamp": ago(48)},
            {"coin": "D", "signal": "dd_result", "timestamp": ago(80)},
            {"coin": "E", "signal": "whale_up"},
        ]
        json.dump(bus, open(path, "w"))
        kept, dropped = base.prune_stale_signals(now)
        left = {s["coin"] for s in json.load(open(path))}
        assert (kept, dropped) == (2, 3), (kept, dropped)
        assert left == {"A", "C"}, left
    _with_tmp_bus(run)


def test_write_signals_refreshes_redetected_signal():
    def run(base, path, json):
        json.dump([{"coin": "X", "source": "s", "signal": "whale_up", "agent": "whale",
                    "confidence": 0.5, "timestamp": "2026-01-01T00:00:00+00:00"}], open(path, "w"))
        agent = base.BaseAgent.__new__(base.BaseAgent)
        agent.name = "whale"
        base.BaseAgent.write_signals(agent, [
            {"coin": "X", "source": "s", "signal": "whale_up", "confidence": 0.8},
            {"coin": "Y", "source": "s", "signal": "whale_up", "confidence": 0.6},
        ])
        bus = json.load(open(path))
        assert len(bus) == 2
        x = [b for b in bus if b["coin"] == "X"][0]
        assert x["confidence"] == 0.8 and not x["timestamp"].startswith("2026-01-01")
    _with_tmp_bus(run)


# ── Prices for paper trading ───────────────────────────────────────────────

def test_price_parsers():
    from supercrypto.core.prices import parse_binance, parse_kucoin
    assert parse_binance([{"symbol": "BTCUSDT", "price": "62000.5"},
                          {"symbol": "ETHBTC", "price": "0.05"},
                          {"symbol": "BADUSDT", "price": "x"}]) == {"BTC": 62000.5}
    assert parse_kucoin({"data": {"ticker": [{"symbol": "PONS-USDT", "last": "0.12"},
                                             {"symbol": "PONS-BTC", "last": "1"}]}}) == {"PONS": 0.12}


def test_get_usd_prices_falls_back_binance_kucoin_coingecko():
    import supercrypto.core.prices as P
    calls = []

    def fake_get(url, params=None, tries=2):
        calls.append(url)
        if url == P.BINANCE_ALL_PRICES:
            return [{"symbol": "SANDUSDT", "price": "0.3"}, {"symbol": "POLUSDT", "price": "0.4"}]
        if url == P.KUCOIN_ALL_TICKERS:
            return {"data": {"ticker": [{"symbol": "PONS-USDT", "last": "0.12"}]}}
        if url.endswith("/simple/price"):
            assert params["ids"] == "edel"
            return {"edel": {"usd": 0.05}}
        return None

    orig = (P.api_get, P.coin_id_for)
    P.api_get = fake_get
    P.coin_id_for = lambda s: {"EDEL": "edel"}.get(s)
    try:
        px = P.get_usd_prices(["SAND", "matic", "PONS", "EDEL", "USDT", "NOPE"], log=lambda *a: None)
    finally:
        P.api_get, P.coin_id_for = orig
    assert px == {"SAND": 0.3, "MATIC": 0.4, "PONS": 0.12, "EDEL": 0.05, "USDT": 1.0}, px


def test_paper_trader_opens_buy_when_priced():
    from supercrypto.core.paper import PaperTrader
    pt = PaperTrader(path=None)
    pt.process([{"coin": "SAND", "action": "BUY", "suggested_size_pct": 4.0,
                 "stop_loss_pct": 15, "take_profit_pct": 45}], {"SAND": 0.5})
    assert "SAND" in pt.state["positions"]
    assert abs(pt.state["cash"] - 96.0) < 1e-6

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
