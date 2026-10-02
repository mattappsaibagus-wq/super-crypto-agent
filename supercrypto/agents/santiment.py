"""Santiment activity agent — catches on-chain / dev activity before price moves.

Works without an API key: `dev_activity_1d` and `daily_active_addresses` are
free and real-time on Santiment's public GraphQL API. With SANTIMENT_API_KEY
set it also tries `social_volume_total` (restricted on the free tier, so it is
skipped automatically if the API refuses it).

Budget: one call per metric per scan, all coins batched in a single
`timeseriesDataPerSlug` request, plus a weekly slug-list refresh. A monthly
call counter in memory stops the agent before the 1,000-call free limit.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone

from supercrypto.config import (
    SANTIMENT_API_KEY,
    SANTIMENT_DAA_FADE,
    SANTIMENT_DAA_SPIKE,
    SANTIMENT_DEV_RATIO,
    SANTIMENT_GRAPHQL,
    SANTIMENT_MAX_SLUGS,
    SANTIMENT_MONTHLY_CALL_CAP,
    SANTIMENT_SOCIAL_SPIKE,
    KNOWN_IDS,
)
from supercrypto.core.base import BaseAgent, api_post, fetch_markets

SLUG_TTL_SECONDS = 7 * 86400
STALE_DAYS = 3
SKIP = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE"}


# ── Pure analysis helpers (unit-tested) ───────────────────────────────────

def dev_activity_ratio(values):
    """Last-7-day dev activity vs the average 7-day block of the prior 21 days."""
    vals = [v for v in values if v is not None]
    if len(vals) < 28:
        return None, 0.0
    last7 = sum(vals[-7:])
    prior = vals[-28:-7]
    base = sum(prior) / 3.0
    if base <= 0:
        return None, last7
    return last7 / base, last7


def spike_ratio(values, lookback=14):
    """Latest value vs the median of the previous `lookback` values."""
    vals = [v for v in values if v is not None]
    if len(vals) < lookback + 1:
        return None
    base = statistics.median(vals[-(lookback + 1):-1])
    if base <= 0:
        return None
    return vals[-1] / base


def analyse(series: dict) -> list:
    """series: {metric: [values oldest→newest]} → list of (signal, confidence, reason)."""
    out = []
    dev = series.get("dev_activity_1d")
    if dev:
        ratio, last7 = dev_activity_ratio(dev)
        if ratio and ratio >= SANTIMENT_DEV_RATIO and last7 >= 20:
            conf = min(0.8, 0.5 + (ratio - SANTIMENT_DEV_RATIO) * 0.2)
            out.append(("dev_activity_up", conf,
                        "dev activity {:.1f}x its 3-week pace (Santiment)".format(ratio)))
    daa = series.get("daily_active_addresses")
    if daa:
        r = spike_ratio(daa)
        if r and r >= SANTIMENT_DAA_SPIKE:
            conf = min(0.8, 0.55 + (r - SANTIMENT_DAA_SPIKE) * 0.1)
            out.append(("active_addresses_spike", conf,
                        "active addresses {:.1f}x 2-week median (Santiment)".format(r)))
        elif r and r <= SANTIMENT_DAA_FADE:
            out.append(("active_addresses_fade", 0.5,
                        "active addresses down to {:.0%} of 2-week median (Santiment)".format(r)))
    soc = series.get("social_volume_total")
    if soc:
        r = spike_ratio(soc)
        if r and r >= SANTIMENT_SOCIAL_SPIKE:
            conf = min(0.8, 0.5 + (r - SANTIMENT_SOCIAL_SPIKE) * 0.05)
            out.append(("social_spike", conf,
                        "social volume {:.1f}x 2-week median (Santiment)".format(r)))
    return out


class SantimentActivity(BaseAgent):
    NAME = "santiment"
    EMOJI = "🛰️"

    def default_weights(self):
        return {
            "dev_activity_up": 0.35,
            "active_addresses_spike": 0.45,
            "active_addresses_fade": 0.30,
            "social_spike": 0.40,
        }

    # ── API plumbing ─────────────────────────────────────────────────
    def _budget_ok(self) -> bool:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        usage = self.memory.setdefault("api_usage", {})
        if usage.get("month") != month:
            usage.clear()
            usage.update({"month": month, "calls": 0})
        return usage["calls"] < SANTIMENT_MONTHLY_CALL_CAP

    def _query(self, query: str):
        if not self._budget_ok():
            print("  🛰️ santiment — monthly call cap reached, skipping")
            return None
        self.memory["api_usage"]["calls"] += 1
        headers = {"Authorization": "Apikey " + SANTIMENT_API_KEY} if SANTIMENT_API_KEY else None
        data = api_post(SANTIMENT_GRAPHQL, {"query": query}, headers=headers)
        if not isinstance(data, dict):
            return None
        if data.get("errors"):
            msg = str(data["errors"][0].get("message", ""))[:120]
            print("  🛰️ santiment — API refused:", msg)
        return data.get("data")

    def _slug_map(self) -> dict:
        """Ticker → Santiment slug, refreshed weekly (one call)."""
        cache = self.memory.get("slug_cache") or {}
        if cache.get("map") and time.time() - cache.get("t", 0) < SLUG_TTL_SECONDS:
            return cache["map"]
        data = self._query("{ allProjects { slug ticker } }") or {}
        projects = data.get("allProjects") or []
        mapping = {}
        cg_ids = set(KNOWN_IDS.values())
        for p in projects:
            t = (p.get("ticker") or "").upper()
            slug = p.get("slug")
            if not t or not slug:
                continue
            # First hit wins (list is market-cap ordered), unless a later slug
            # matches a known CoinGecko id exactly.
            if t not in mapping or (slug in cg_ids and mapping[t] not in cg_ids):
                mapping[t] = slug
        if mapping:
            self.memory["slug_cache"] = {"t": time.time(), "map": mapping}
            return mapping
        return cache.get("map") or {}

    def _fetch_metric(self, metric: str, slugs: list) -> dict:
        q = (
            '{ getMetric(metric:"%s"){ timeseriesDataPerSlug('
            'selector:{slugs:[%s]} from:"utc_now-30d" to:"utc_now" interval:"1d")'
            '{ datetime data { slug value } } } }'
        ) % (metric, ",".join('"%s"' % s for s in slugs))
        data = self._query(q) or {}
        rows = ((data.get("getMetric") or {}).get("timeseriesDataPerSlug")) or []
        out, last_seen = {}, None
        for row in rows:
            last_seen = row.get("datetime") or last_seen
            for d in row.get("data") or []:
                out.setdefault(d.get("slug"), []).append(d.get("value"))
        if last_seen:
            try:
                ts = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - ts).days > STALE_DAYS:
                    return {}  # lagged (restricted) data is useless for early signals
            except ValueError:
                pass
        return out

    # ── Run ─────────────────────────────────────────────────────────
    def _candidates(self, kwargs) -> list:
        coins = kwargs.get("coins")
        if coins:
            return [c.upper() for c in coins]
        counts = {}
        for s in self.read_signals():
            c = (s.get("coin") or "").upper()
            if c and c not in SKIP:
                counts[c] = counts.get(c, 0) + 1
        return [c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    def run(self, **kwargs):
        slug_map = self._slug_map()
        if not slug_map:
            self.save_memory()
            return []
        pairs = []
        for sym in self._candidates(kwargs):
            slug = slug_map.get(sym)
            if slug:
                pairs.append((sym, slug))
            if len(pairs) >= SANTIMENT_MAX_SLUGS:
                break
        if not pairs:
            self.save_memory()
            return []
        slugs = [s for _, s in pairs]
        metrics = ["dev_activity_1d", "daily_active_addresses"]
        if SANTIMENT_API_KEY:
            metrics.append("social_volume_total")
        per_metric = {m: self._fetch_metric(m, slugs) for m in metrics}

        prices = {c.get("symbol", "").upper(): c.get("current_price") for c in fetch_markets()}
        signals = []
        for sym, slug in pairs:
            series = {m: per_metric[m].get(slug) for m in metrics if per_metric[m].get(slug)}
            for name, conf, reason in analyse(series):
                details = {"reasons": [reason], "slug": slug}
                if prices.get(sym):
                    details["price"] = prices[sym]
                signals.append({
                    "coin": sym, "signal": name, "confidence": round(conf, 3),
                    "source": "santiment", "details": details,
                })
        self.save_memory()
        return signals
