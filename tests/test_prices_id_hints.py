from supercrypto.core import prices as P


def test_ids_from_bus():
    bus = [{"coin": "hi", "details": {"coin_id": "hi-dollar"}},
           {"coin": "BTC", "details": {}}]
    assert P.ids_from_bus(bus) == {"HI": "hi-dollar"}


def test_id_hint_beats_ambiguous_lookup(monkeypatch):
    calls = {}

    def fake_get(url, params=None, **kw):
        if "simple/price" in url:
            calls["ids"] = params["ids"]
            return {"hi-dollar": {"usd": 4e-05}}
        return []  # no exchange lists it

    monkeypatch.setattr(P, "api_get", fake_get)
    monkeypatch.setattr(P, "coin_id_for", lambda c: "some-namesake")
    out = P.get_usd_prices(["HI"], log=lambda *a: None, id_hints={"HI": "hi-dollar"})
    assert calls["ids"] == "hi-dollar"
    assert out == {"HI": 4e-05}


def test_hi_is_pinned():
    from supercrypto.config import KNOWN_IDS
    assert KNOWN_IDS["HI"] == "hi-dollar"


def test_coingecko_failure_is_reported(monkeypatch):
    monkeypatch.setattr(P, "api_get", lambda url, params=None, **kw: None if "simple" in url else [])
    errs = []
    out = P.get_usd_prices(["HI"], log=lambda *a: None, id_hints={"HI": "hi-dollar"}, errors=errs)
    assert out == {} and "request failed" in errs[0]
    from supercrypto.core import health as H
    c = H.check_price_coverage(["HI"], {}, [], [], errs)
    assert "CoinGecko request failed" in c["detail"]
