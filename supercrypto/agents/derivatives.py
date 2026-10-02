"""Derivatives flow agent — Hyperliquid perp funding + open interest.

Buildix (buildix.trade) is an analytics layer over Hyperliquid; its API is
paid, but the underlying data is on Hyperliquid's free public `info`
endpoint. One POST returns funding, open interest, mark price and 24h volume
for every perp, so this agent costs a single request per scan.

Open-interest change is measured against the snapshot saved on the previous
scan (normally 6h earlier), kept in this agent's memory.
"""

from __future__ import annotations

import time

from supercrypto.config import (
    HL_FUNDING_HOT,
    HL_FUNDING_SQUEEZE,
    HL_MAX_SIGNALS,
    HL_MIN_DAY_VOLUME_USD,
    HL_MIN_OI_USD,
    HL_OI_BUILDUP_PCT,
    HL_OI_FLUSH_PCT,
    HYPERLIQUID_INFO,
)
from supercrypto.core.base import BaseAgent, api_post

SNAP_MIN_AGE = 2 * 3600
SNAP_MAX_AGE = 30 * 3600


def normalise(name: str):
    """Hyperliquid 'kPEPE' is priced per 1,000 tokens → ('PEPE', 1000)."""
    if len(name) > 1 and name[0] == "k" and name[1:].isupper():
        return name[1:], 1000.0
    return name.upper(), 1.0


def parse_markets(payload) -> dict:
    """metaAndAssetCtxs response → {symbol: {...}} with USD figures."""
    if not isinstance(payload, list) or len(payload) < 2:
        return {}
    universe = (payload[0] or {}).get("universe") or []
    ctxs = payload[1] or []
    out = {}
    for meta, ctx in zip(universe, ctxs):
        if meta.get("isDelisted") or not ctx:
            continue
        try:
            mark = float(ctx.get("markPx") or 0)
            prev = float(ctx.get("prevDayPx") or 0)
            oi = float(ctx.get("openInterest") or 0)
            funding = float(ctx.get("funding") or 0)
            vol = float(ctx.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            continue
        if mark <= 0:
            continue
        sym, mult = normalise(meta.get("name", ""))
        out[sym] = {
            "price": mark / mult,
            "chg24h": (mark - prev) / prev * 100 if prev else 0.0,
            "oi_usd": oi * mark,
            "funding": funding,
            "vol_usd": vol,
        }
    return out


def evaluate(sym: str, m: dict, prev: dict = None) -> list:
    """Return [(signal, confidence, reason)] for one market."""
    if m["oi_usd"] < HL_MIN_OI_USD or m["vol_usd"] < HL_MIN_DAY_VOLUME_USD:
        return []
    out = []
    f = m["funding"]
    apr = f * 24 * 365 * 100
    if f >= HL_FUNDING_HOT:
        conf = min(0.8, 0.55 + (f / HL_FUNDING_HOT - 1) * 0.1)
        out.append(("funding_overheated", conf,
                    "longs paying {:.0f}% APR funding — crowded (Hyperliquid)".format(apr)))
    elif f <= HL_FUNDING_SQUEEZE and m["chg24h"] > 0:
        conf = min(0.8, 0.55 + (f / HL_FUNDING_SQUEEZE - 1) * 0.1)
        out.append(("funding_squeeze", conf,
                    "shorts paying {:.0f}% APR while price +{:.1f}% — squeeze setup (Hyperliquid)".format(
                        abs(apr), m["chg24h"])))
    if prev and prev.get("oi_usd") and prev.get("price"):
        oi_chg = (m["oi_usd"] - prev["oi_usd"]) / prev["oi_usd"] * 100
        px_chg = (m["price"] - prev["price"]) / prev["price"] * 100
        if oi_chg >= HL_OI_BUILDUP_PCT and px_chg >= 2:
            conf = min(0.8, 0.5 + oi_chg / 200)
            out.append(("oi_buildup", conf,
                        "open interest +{:.0f}% with price +{:.1f}% since last scan (Hyperliquid)".format(
                            oi_chg, px_chg)))
        elif oi_chg <= HL_OI_FLUSH_PCT and px_chg <= -3:
            conf = min(0.75, 0.5 + abs(oi_chg) / 200)
            out.append(("oi_flush", conf,
                        "open interest {:.0f}% with price {:.1f}% — liquidation flush (Hyperliquid)".format(
                            oi_chg, px_chg)))
    return out


class DerivativesFlow(BaseAgent):
    NAME = "derivatives"
    EMOJI = "📉"

    def default_weights(self):
        return {
            "funding_squeeze": 0.40,
            "oi_buildup": 0.40,
            "funding_overheated": 0.35,
            "oi_flush": 0.30,
        }

    def run(self, **kwargs):
        markets = parse_markets(api_post(HYPERLIQUID_INFO, {"type": "metaAndAssetCtxs"}))
        if not markets:
            return []
        snap = self.memory.get("snapshot") or {}
        age = time.time() - snap.get("t", 0)
        prev = snap.get("markets", {}) if SNAP_MIN_AGE <= age <= SNAP_MAX_AGE else {}

        signals = []
        for sym, m in markets.items():
            for name, conf, reason in evaluate(sym, m, prev.get(sym)):
                signals.append({
                    "coin": sym, "signal": name, "confidence": round(conf, 3),
                    "source": "hyperliquid",
                    "details": {
                        "reasons": [reason],
                        "price": m["price"],
                        "funding_hourly": m["funding"],
                        "open_interest_usd": round(m["oi_usd"]),
                    },
                })
        signals.sort(key=lambda s: -s["confidence"])

        # Only roll the snapshot forward once it is old enough, so OI change is
        # always measured over a meaningful window even with manual re-runs.
        if age >= SNAP_MIN_AGE:
            self.memory["snapshot"] = {
                "t": time.time(),
                "markets": {s: {"oi_usd": m["oi_usd"], "price": m["price"]}
                            for s, m in markets.items()},
            }
        self.save_memory()
        return signals[:HL_MAX_SIGNALS]
