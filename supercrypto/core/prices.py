"""Current USD prices for a set of tickers, with exchange fallbacks.

Paper trading used to price coins only through CoinGecko (no API key, plus a
ticker->id lookup that is itself a CoinGecko call). On GitHub's shared runners
that is regularly rate-limited, every BUY was skipped for lack of a price, and
the paper portfolio never opened a position.

Order: Binance (one call for every pair) -> KuCoin (one call for every pair)
-> CoinGecko simple/price with the demo key for whatever is still missing.
"""

from __future__ import annotations

from typing import Iterable

from supercrypto.config import COINGECKO_API_KEY, COINGECKO_BASE
from supercrypto.core.base import api_get, coin_id_for

BINANCE_ALL_PRICES = "https://data-api.binance.vision/api/v3/ticker/price"
KUCOIN_ALL_TICKERS = "https://api.kucoin.com/api/v1/market/allTickers"
STABLES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "BUSD", "PYUSD", "USDS"}
ALIASES = {"MATIC": "POL"}  # renamed on the exchanges
# An exchange ticker can belong to a different token than the one the agents
# flagged (e.g. Binance BEAMUSDT vs CoinGecko's Beam). If the exchange price is
# outside this ratio of the CoinGecko-derived reference, it's the wrong asset.
MAX_REF_RATIO = 1.33


def price_matches(px, ref, max_ratio=MAX_REF_RATIO) -> bool:
    if not px or not ref:
        return True  # nothing to compare against
    r = px / ref
    return 1 / max_ratio <= r <= max_ratio


def reference_from_bus(signals) -> dict:
    """{TICKER: median CoinGecko-derived price} from bus signal details.

    Kronos signals are excluded: their price can come from the exchange
    ticker, which is exactly what is being checked."""
    import statistics
    seen = {}
    for s in signals or []:
        if s.get("agent") == "kronos":
            continue
        px = (s.get("details") or {}).get("price")
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if px > 0:
            seen.setdefault((s.get("coin") or "").upper(), []).append(px)
    return {c: statistics.median(v) for c, v in seen.items() if c}


def ids_from_bus(signals) -> dict:
    """{TICKER: CoinGecko id} from bus signal details (microcap/dd carry it)."""
    out = {}
    for s in signals or []:
        cid = (s.get("details") or {}).get("coin_id")
        coin = (s.get("coin") or "").upper()
        if coin and cid and coin not in out:
            out[coin] = cid
    return out


def parse_binance(data) -> dict:
    """[{symbol: 'BTCUSDT', price: '123'}...] -> {'BTC': 123.0} (USDT pairs only)."""
    out = {}
    for row in data if isinstance(data, list) else []:
        sym = str(row.get("symbol", ""))
        if not sym.endswith("USDT"):
            continue
        try:
            px = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if px > 0:
            out[sym[:-4]] = px
    return out


def parse_kucoin(data) -> dict:
    """{data: {ticker: [{symbol: 'BTC-USDT', last: '123'}...]}} -> {'BTC': 123.0}."""
    rows = ((data or {}).get("data") or {}).get("ticker") if isinstance(data, dict) else None
    out = {}
    for row in rows or []:
        sym = str(row.get("symbol", ""))
        if not sym.endswith("-USDT"):
            continue
        try:
            px = float(row.get("last"))
        except (TypeError, ValueError):
            continue
        if px > 0:
            out[sym[:-5]] = px
    return out


def get_usd_prices(coins: Iterable[str], log=print, reference=None, mismatches=None,
                   id_hints=None) -> dict:
    """Return {TICKER: usd_price} for as many of `coins` as any source knows.

    reference: {TICKER: CoinGecko-derived price}; exchange prices that don't
    match it are rejected (and the ticker added to `mismatches`, if given).
    id_hints: {TICKER: CoinGecko id} known for the exact token (e.g. stored on
    a paper position), used instead of the ambiguous ticker->id lookup."""
    reference = reference or {}
    id_hints = {k.upper(): v for k, v in (id_hints or {}).items() if v}
    wanted = []
    for c in coins:
        c = (c or "").upper()
        if c and c not in wanted:
            wanted.append(c)
    if not wanted:
        return {}

    prices, source = {}, {}
    for c in wanted:
        if c in STABLES:
            prices[c], source[c] = 1.0, "stable"

    def fill(table, name):
        for c in wanted:
            if c in prices:
                continue
            px = table.get(ALIASES.get(c, c)) or table.get(c)
            if not px:
                continue
            if not price_matches(px, reference.get(c)):
                if mismatches is not None:
                    mismatches.add(c)
                continue
            prices[c], source[c] = px, name

    fill(parse_binance(api_get(BINANCE_ALL_PRICES)), "binance")
    if len(prices) < len(wanted):
        fill(parse_kucoin(api_get(KUCOIN_ALL_TICKERS)), "kucoin")

    missing = [c for c in wanted if c not in prices]
    if missing:
        ids = {}
        for c in missing:
            cid = id_hints.get(c) or coin_id_for(c)
            if cid:
                ids[cid] = c
        if ids:
            params = {"ids": ",".join(ids), "vs_currencies": "usd"}
            if COINGECKO_API_KEY:
                params["x_cg_demo_api_key"] = COINGECKO_API_KEY
            data = api_get(f"{COINGECKO_BASE}/simple/price", params=params) or {}
            for cid, c in ids.items():
                px = (data.get(cid) or {}).get("usd") if isinstance(data, dict) else None
                # ticker->id lookup can pick a namesake too; same check
                if px and price_matches(float(px), reference.get(c)):
                    prices[c], source[c] = float(px), "coingecko"
    for c in missing:  # last resort: the reference itself (CoinGecko via the bus)
        if c not in prices and reference.get(c):
            prices[c], source[c] = float(reference[c]), "reference"

    counts = {}
    for s in source.values():
        counts[s] = counts.get(s, 0) + 1
    missing = [c for c in wanted if c not in prices]
    log("    prices: %d/%d %s%s%s" % (
        len(prices), len(wanted), counts,
        (" | no price: " + ", ".join(missing[:15])) if missing else "",
        (" | ticker mismatch (wrong asset on exchange): " + ", ".join(sorted(mismatches)))
        if mismatches else ""))
    return prices
