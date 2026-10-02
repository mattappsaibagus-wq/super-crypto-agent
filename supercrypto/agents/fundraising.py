"""Fundraising agent — fresh VC rounds for tradable tokens.

Reads the public deal-flow table on crypto-fundraising.info (the newest ~10
rounds are visible without an account; their full API is contract-only).
A round signals `fresh_funding` only when the project has a ticker that is
already tradable, and only for FUNDRAISING_FRESH_DAYS after we first see it,
so the same deal does not keep boosting a coin forever.

This scrapes HTML, so it is deliberately low-weight and fails quietly if the
page layout changes.
"""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser

import requests

from supercrypto.config import API_TIMEOUT, FUNDRAISING_DEALFLOW, FUNDRAISING_FRESH_DAYS
from supercrypto.core.base import BaseAgent, fetch_markets

TOP_TIER_VCS = (
    "a16z", "andreessen", "paradigm", "multicoin", "coinbase ventures", "polychain",
    "binance labs", "yzi labs", "pantera", "sequoia", "dragonfly", "framework",
    "jump", "delphi", "hashed", "animoca", "galaxy", "electric capital", "variant",
)
COLUMNS = ["num", "project", "round", "date", "raised", "fdv", "tradable", "category", "investors"]


class _DealParser(HTMLParser):
    """Collects text of each child cell of every `div.hpt-data` row."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._depth = 0          # div depth inside the current row
        self._row = None
        self._cell = None
        self._in_tag = False     # inside span.cointag

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if self._row is None:
            if tag == "div" and "hpt-data" in cls.split():
                self._row = {"cells": [], "ticker": None, "href": None}
                self._depth = 1
            return
        if tag == "div":
            self._depth += 1
            if self._depth == 2:
                self._cell = []
        if tag == "a" and "t-project-link" in cls:
            self._row["href"] = dict(attrs).get("href")
        if tag == "span" and "cointag" in cls.split():
            self._in_tag = True

    def handle_endtag(self, tag):
        if self._row is None:
            return
        if tag == "span":
            self._in_tag = False
        if tag == "div":
            if self._depth == 2 and self._cell is not None:
                self._row["cells"].append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
                self._cell = None
            self._depth -= 1
            if self._depth == 0:
                self.rows.append(self._row)
                self._row = None

    def handle_data(self, data):
        if self._row is None:
            return
        if self._in_tag and data.strip():
            self._row["ticker"] = data.strip().upper()
        if self._cell is not None:
            self._cell.append(data + " ")


def parse_amount(text: str) -> float:
    m = re.search(r"\$\s*([\d.,]+)\s*([kKmMbB]?)", text or "")
    if not m:
        return 0.0
    val = float(m.group(1).replace(",", ""))
    return val * {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2).lower(), 1)


def parse_deals(html: str) -> list:
    p = _DealParser()
    p.feed(html or "")
    deals = []
    for r in p.rows:
        cells = dict(zip(COLUMNS, r["cells"]))
        if not cells.get("project"):
            continue
        ticker = r["ticker"]
        name = cells["project"]
        if ticker and name.upper().endswith(" " + ticker):
            name = name[: -len(ticker)].strip()
        investors = cells.get("investors", "").replace("Investors:", "").strip()
        deals.append({
            "project": name,
            "ticker": ticker,
            "round": cells.get("round", ""),
            "date": cells.get("date", ""),
            "raised_usd": parse_amount(cells.get("raised", "")),
            "tradable": cells.get("tradable", "").strip().lower() == "yes",
            "category": cells.get("category", ""),
            "investors": investors,
            "href": r["href"],
        })
    return deals


def score_deal(deal: dict) -> float:
    conf = 0.5
    raised = deal.get("raised_usd") or 0
    if raised >= 50e6:
        conf += 0.15
    elif raised >= 10e6:
        conf += 0.1
    elif raised >= 3e6:
        conf += 0.05
    inv = (deal.get("investors") or "").lower()
    if any(v in inv for v in TOP_TIER_VCS):
        conf += 0.1
    return min(conf, 0.8)


class FundraisingAgent(BaseAgent):
    NAME = "fundraising"
    EMOJI = "💰"

    def default_weights(self):
        return {"fresh_funding": 0.25}

    def _fetch(self) -> str:
        try:
            r = requests.get(
                FUNDRAISING_DEALFLOW, timeout=API_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (super-crypto-agent research bot)"},
            )
            return r.text if r.status_code == 200 else ""
        except requests.exceptions.RequestException:
            return ""

    def run(self, **kwargs):
        html = kwargs.get("html") or self._fetch()
        deals = parse_deals(html)
        if not deals:
            print("  💰 fundraising — no deals parsed (page down or layout changed)")
            return []
        seen = self.memory.setdefault("seen_deals", {})
        now = time.time()
        for d in deals:
            key = "{}|{}|{}".format(d["project"], d["round"], d["date"])
            seen.setdefault(key, now)
        # Forget entries older than 90 days to keep memory small.
        for k in [k for k, t in seen.items() if now - t > 90 * 86400]:
            del seen[k]

        prices = {c.get("symbol", "").upper(): c.get("current_price") for c in fetch_markets()}
        signals = []
        for d in deals:
            if not (d["ticker"] and d["tradable"]):
                continue
            key = "{}|{}|{}".format(d["project"], d["round"], d["date"])
            if now - seen[key] > FUNDRAISING_FRESH_DAYS * 86400:
                continue
            amount = "${:,.1f}M".format(d["raised_usd"] / 1e6) if d["raised_usd"] else "undisclosed"
            reason = "{} round {} ({}){}".format(
                d["round"], amount, d["date"],
                " — investors: " + d["investors"][:60] if d["investors"] else "")
            details = {"reasons": [reason], "project": d["project"], "round": d["round"],
                       "raised_usd": d["raised_usd"], "investors": d["investors"]}
            if prices.get(d["ticker"]):
                details["price"] = prices[d["ticker"]]
            signals.append({
                "coin": d["ticker"], "signal": "fresh_funding",
                "confidence": round(score_deal(d), 3),
                "source": "crypto-fundraising", "details": details,
            })
        self.save_memory()
        return signals
