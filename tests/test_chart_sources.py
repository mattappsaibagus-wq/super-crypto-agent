"""Unit tests for the KuCoin chart data source added as a fallback to Binance and CoinGecko.

These verify the KuCoin helpers are wired correctly and that the /api/chart and
/api/ohlc endpoints fall through to KuCoin when Binance and CoinGecko return no
data. This is the fix for coins such as BRETT, MEW and MELANIA that used to show
"Chart unavailable" because CoinGecko's free tier throttled them under a burst.

Run from the repo root:  pytest -q tests/test_chart_sources.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import server as s


@pytest.fixture()
def client():
    s.app.config["TESTING"] = True
    with s.app.test_client() as c:
        yield c


# ── _kucoin_klines parsing ─────────────────────────────────────────────────

def test_kucoin_klines_code_200000(monkeypatch):
    """Returns rows when KuCoin replies with code 200000."""
    monkeypatch.setattr(s, "api_get",
                        lambda url, params=None, tries=None:
                        {"code": "200000", "data": [[1000, 40000, 40050, 40200, 39980, 10.5, 420.0]]})
    assert s._kucoin_klines("TESTUSDT", "1day") == [[1000, 40000, 40050, 40200, 39980, 10.5, 420.0]]


def test_kucoin_klines_error_code(monkeypatch):
    """Returns [] when KuCoin replies with an error code."""
    monkeypatch.setattr(s, "api_get",
                        lambda url, params=None, tries=None:
                        {"code": "400100", "msg": "invalid"})
    assert s._kucoin_klines("BADUSDT", "1day") == []


def test_kucoin_klines_none(monkeypatch):
    """Returns [] when the HTTP call fails (None, network error)."""
    monkeypatch.setattr(s, "api_get", lambda url, params=None, tries=None: None)
    assert s._kucoin_klines("TESTUSDT", "1day") == []


# ── KuCoin data mappers ────────────────────────────────────────────────────

def test_kucoin_chart_prices_maps_ms_and_close(monkeypatch):
    """Builds close-price series with ms timestamps; drops non-positive closes."""
    monkeypatch.setattr(s, "_kucoin_klines",
                        lambda p, k, tries=None:
                        [[1609459200, 40000, 40100, 40200, 39980, 10.0, 400.0]])
    assert s._kucoin_chart_prices("BTC", "1d") == [{"t": 1609459200000, "price": 40100.0}]

    monkeypatch.setattr(s, "_kucoin_klines",
                        lambda p, k, tries=None:
                        [[1609459200, 40000, 0, 40200, 39980, 0, 0]])
    assert s._kucoin_chart_prices("BTC", "1d") == []


def test_kucoin_ohlc_reorders_fields(monkeypatch):
    """Maps KuCoin [time, open, close, high, low, volume] -> o/h/l/c/v with ms time."""
    monkeypatch.setattr(s, "_kucoin_klines",
                        lambda p, k, tries=None:
                        [[1609459200, 39900, 40100, 40200, 39980, 5.5]])
    assert s._kucoin_ohlc("BTC", "1d") == [{
        "t": 1609459200000, "o": 39900.0, "h": 40200.0, "l": 39980.0,
        "c": 40100.0, "v": 5.5,
    }]


# ── Endpoint fall-through ──────────────────────────────────────────────────

def test_chart_endpoint_uses_kucoin_when_binance_and_cg_empty(monkeypatch, client):
    """BRETT gets a KuCoin chart when Binance and CoinGecko return nothing."""
    monkeypatch.setattr(s, "_binance_chart", lambda sym, iv: [])
    monkeypatch.setattr(s, "_kucoin_chart_prices", lambda sym, iv:
                        [{"t": 1609459200000, "price": 50000.0},
                         {"t": 1609545600000, "price": 50200.0}])
    monkeypatch.setattr(s, "_cg_id_for_coin", lambda sym: "based-brett")
    monkeypatch.setattr(s, "api_get", lambda url, params=None, tries=None:
                        {"error": "rate limited"})

    resp = client.get("/api/chart/BRETT?interval=7d")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "kucoin"
    assert len(data["prices"]) == 2
    assert data["prices"][0]["price"] == 50000.0


def test_ohlc_endpoint_uses_kucoin_when_binance_and_cg_empty(monkeypatch, client):
    """MEW gets KuCoin OHLC when Binance and CoinGecko return nothing."""
    monkeypatch.setattr(s, "_binance_chart_ohlc", lambda sym, iv: [])
    monkeypatch.setattr(s, "_kucoin_ohlc", lambda sym, iv:
                        [{"t": 1609459200000, "o": 49000, "h": 50500, "l": 48900,
                          "c": 50000.0, "v": 100},
                         {"t": 1609545600000, "o": 50000, "h": 51000, "l": 49800,
                          "c": 50800.0, "v": 120}])
    monkeypatch.setattr(s, "_coingecko_ohlc", lambda sym, iv: [])

    resp = client.get("/api/ohlc/MEW?interval=30d")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "kucoin"
    assert len(data["candles"]) == 2
    assert data["candles"][0]["c"] == 50000.0