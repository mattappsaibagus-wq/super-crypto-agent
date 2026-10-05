#!/usr/bin/env python3
"""Super Crypto Agent — Premium Web Dashboard.

Run locally:
    python3 server.py                # http://localhost:8080
    python3 server.py --port 9000

Render auto-injects $PORT — just point your web service at this file.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template_string, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from supercrypto.config import JST, KNOWN_IDS, now_jst
from supercrypto.core.base import api_get, coin_id_for

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_BASE = "https://data-api.binance.vision/api/v3"
# Free, no-key candlestick source used as a middle-tier fallback for coins not
# on Binance. CoinGecko's free tier (no COINGECKO_API_KEY) throttles the newer
# meme/alt coins under a multi-coin burst; KuCoin serves those same pairs cheaply.
KUCOIN_BASE = "https://api.kucoin.com/api/v1"
CG_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()

# Shared in-memory caches
_chart_cache = {}
_market_cache = {}

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")
ATTRIB_DIR = os.path.join(DATA_DIR, "attribution")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
_cache_file = os.path.join(DATA_DIR, "market_cache.json")
VERDICTS_FILE = os.path.join(DATA_DIR, "verdicts.json")
KRONOS_FILE = os.path.join(DATA_DIR, "kronos_forecasts.json")


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


PAPER_FILE = os.path.join(DATA_DIR, "paper_portfolio.json")


def get_portfolio_summary():
    """Paper portfolio: open positions (marked at last scan), closed trades,
    headline metrics and a de-duplicated equity curve."""
    from supercrypto.core.paper import PaperTrader

    state = _read_json(PAPER_FILE, {}) or {}
    if not state:
        return {"available": False}
    pt = PaperTrader(path=None)
    pt.state = dict(pt._empty(), **state)
    metrics = pt.metrics()
    start = state.get("starting_equity") or 100.0
    positions = []
    for coin, pos in (state.get("positions") or {}).items():
        entry = pos.get("entry_price") or 0
        last = pos.get("last_price") or entry
        qty = pos.get("qty") or 0
        positions.append({
            "coin": coin,
            "entry_price": entry,
            "last_price": last,
            "qty": qty,
            "size_pct": round(pos.get("size_pct") or 0, 2),
            "cost": round(qty * entry, 4),
            "value": round(qty * last, 4),
            "pnl_pct": round((last - entry) / entry * 100, 2) if entry else None,
            "stop_price": entry * (1 - abs(pos.get("stop_loss_pct") or 0) / 100) if pos.get("stop_loss_pct") else None,
            "target_price": entry * (1 + abs(pos.get("take_profit_pct") or 0) / 100) if pos.get("take_profit_pct") else None,
            "stop_loss_pct": pos.get("stop_loss_pct"),
            "take_profit_pct": pos.get("take_profit_pct"),
            "opened_at": pos.get("opened_at"),
        })
    positions.sort(key=lambda p: p["opened_at"] or "")
    curve, last_eq = [], None
    for pt_ in state.get("equity_curve") or []:
        eq = pt_.get("equity")
        if eq != last_eq or (curve and pt_.get("t", "")[:13] != curve[-1]["t"][:13]):
            curve.append({"t": pt_.get("t"), "equity": eq})
            last_eq = eq
    closed = list(state.get("closed") or [])[-20:][::-1]
    return {
        "available": True,
        "starting_equity": start,
        "equity": state.get("equity"),
        "cash": round(state.get("cash") or 0, 4),
        "return_pct": round(((state.get("equity") or start) - start) / start * 100, 2),
        "metrics": metrics,
        "positions": positions,
        "closed": closed,
        "equity_curve": curve[-300:],
    }


def get_kronos_summary():
    """Latest Kronos forecasts + graded track record for the dashboard."""
    store = _read_json(KRONOS_FILE, {}) or {}
    stats = store.get("stats") or {}
    return {
        "available": bool(store.get("latest")) and not stats.get("unavailable"),
        "stats": stats,
        "track_record": store.get("track_record") or {},
        "latest": store.get("latest") or {},
        "recent": [r for r in (store.get("history") or []) if r.get("graded")][-40:][::-1],
    }


def load_market_cache():
    """Load market cache from disk if it exists."""
    global _market_cache
    if os.path.exists(_cache_file):
        try:
            with open(_cache_file) as f:
                _market_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass


def save_market_cache():
    """Save market cache to disk for persistence across restarts."""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(_cache_file, "w") as f:
            json.dump(_market_cache, f)
    except (OSError, TypeError):
        pass


def prewarm_market_cache():
    """Pre-fetch top markets so coin lookups survive CoinGecko rate limits."""
    params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "sparkline": "false"}
    if CG_API_KEY:
        params["x_cg_demo_api_key"] = CG_API_KEY
    data = api_get(f"{COINGECKO_BASE}/coins/markets", params=params, tries=3)
    if isinstance(data, list) and data:
        for c in data:
            sym = c.get("symbol", "").upper()
            _market_cache[sym] = c
        save_market_cache()


def prewarm_microcap_cache():
    """Fetch market data for microcaps not in top-250 markets.

    Previously issued one /coins/{id} request per missing coin, each
    followed by a flat 1.5s sleep - for a report with 30+ microcaps that
    alone was 45-90+ seconds. CoinGecko's /coins/markets endpoint accepts
    a comma-separated `ids` parameter and returns up to 250 coins in a
    single response, so this now does one batched call (chunked at 250
    ids) instead of N sequential ones.
    """
    from supercrypto.core.base import coin_id_for
    md_path, _ = get_latest_report()
    if not md_path:
        return
    cards = parse_report_cards(md_path)

    missing_ids = []
    id_to_symbol = {}
    for card in cards:
        symbol = card.get("coin", "").upper()
        if not symbol or symbol in _market_cache:
            continue
        cid = _cg_id_for_coin(symbol)
        if cid == symbol.lower():
            cid = coin_id_for(symbol) or cid
        if cid:
            missing_ids.append(cid)
            id_to_symbol[cid] = symbol

    if not missing_ids:
        return

    for i in range(0, len(missing_ids), 250):
        chunk = missing_ids[i:i + 250]
        params = {"vs_currency": "usd", "ids": ",".join(chunk),
                  "order": "market_cap_desc", "per_page": 250, "sparkline": "false"}
        if CG_API_KEY:
            params["x_cg_demo_api_key"] = CG_API_KEY
        data = api_get(f"{COINGECKO_BASE}/coins/markets", params=params, tries=3)
        if isinstance(data, list):
            for c in data:
                cid = c.get("id", "")
                symbol = id_to_symbol.get(cid) or c.get("symbol", "").upper()
                _market_cache[symbol] = c
        if i + 250 < len(missing_ids):
            time.sleep(1.0)  # brief pacing only between chunks, not per coin
    save_market_cache()


def prewarm_chart_cache():
    """Pre-fetch 7D chart data for all coins in the latest report.

    Runs Binance-first fetches (the common case) concurrently via a small
    thread pool - Binance's public API tolerates far more concurrency than
    CoinGecko's free tier, so this no longer pays a 1.5s penalty per coin
    for the coins that resolve there. Only the CoinGecko fallback path
    (uncommon - unlisted/new coins) still sleeps between its own calls,
    scoped per-worker so it doesn't serialize the whole batch.
    """
    from concurrent.futures import ThreadPoolExecutor

    md_path, _ = get_latest_report()
    if not md_path:
        return
    cards = parse_report_cards(md_path)
    symbols = [c.get("coin", "").upper() for c in cards if c.get("coin")]
    if not symbols:
        return

    def _prewarm_one(symbol):
        bn_prices = _binance_chart(symbol, "7d")
        if bn_prices and len(bn_prices) >= 2:
            _cached_set(f"chart:{symbol}:7", {
                "symbol": symbol, "interval": "7d", "days": 7,
                "prices": bn_prices, "source": "binance",
            })
            return
        cid = _cg_id_for_coin(symbol)
        params = {"vs_currency": "usd", "days": 7}
        if CG_API_KEY:
            params["x_cg_demo_api_key"] = CG_API_KEY
        data = api_get(f"{COINGECKO_BASE}/coins/{cid}/market_chart", params=params, tries=2)
        if isinstance(data, dict) and "prices" in data:
            prices = [{"t": p[0], "price": round(p[1], 4)} for p in data.get("prices", []) if p[1] > 0]
            _cached_set(f"chart:{symbol}:7", {
                "symbol": symbol, "interval": "7d", "days": 7,
                "prices": prices, "source": "coingecko",
            })
        time.sleep(1.5)  # only paid by coins that actually fell through to CoinGecko

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_prewarm_one, symbols))


def _cached_get(key: str, ttl: int = 60):
    entry = _chart_cache.get(key)
    if entry and time.time() - entry["t"] < ttl:
        return entry["data"]
    return None


def _cached_set(key: str, data):
    _chart_cache[key] = {"data": data, "t": time.time()}

# ── Scan state ──────────────────────────────────────────────────────────
scan_lock = threading.Lock()
scan_running = False
scan_last_run = None
scan_last_error = None


def run_scan_background():
    global scan_running, scan_last_error
    with scan_lock:
        if scan_running:
            return {"status": "already_running"}
        scan_running = True
        scan_last_error = None

    def _worker():
        global scan_running, scan_last_run, scan_last_error
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "run_pipeline.py"), "--quick", "--no-clear"],
                capture_output=True, text=True, timeout=280, cwd=BASE_DIR,
            )
            scan_last_run = now_jst().isoformat(timespec="seconds")
            if result.returncode != 0:
                scan_last_error = result.stderr[-500:] if result.stderr else "unknown error"
        except Exception as e:
            scan_last_error = str(e)
        finally:
            # The scan's actual output (recommendations) is ready at this
            # point. Everything below is cache-warming for coin-detail and
            # chart pages, which already have live-fetch fallbacks for a
            # cache miss (see /api/coin, /api/chart) - it's a nice-to-have
            # speed boost for later page loads, not a requirement, so it no
            # longer holds the UI in "running" state.
            scan_running = False

        try:
            load_market_cache()
            prewarm_market_cache()  # top 250 markets
            prewarm_microcap_cache()  # microcaps in report (batched)
            prewarm_chart_cache()     # 7D charts for report coins (parallelized)
        except Exception:
            pass  # best-effort cache warming; never let this resurrect an error state

    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "started"}


def get_latest_report():
    files = sorted(glob.glob(os.path.join(REPORTS_DIR, "report_*.md")))
    if not files:
        return None, None
    with open(files[-1], "r") as f:
        return f.read(), files[-1]


def parse_report_cards(md):
    cards = []
    current = None
    for line in md.split("\n"):
        line = line.strip()
        if line.startswith("## "):
            if current:
                cards.append(current)
            title = line[3:].strip()
            action = "WATCH"
            if "BUY" in title:
                action = "BUY"
            elif "HOLD" in title:
                action = "HOLD"
            elif "AVOID" in title:
                action = "AVOID"
            coin = title.split("—")[0].strip() if "—" in title else title
            coin = coin.lstrip("🟢🟡🔴 ").strip()
            current = {"coin": coin, "action": action, "details": [], "raw": ""}
        elif current and line.startswith("- **"):
            current["details"].append(line[2:].strip())
            current["raw"] += line[2:] + "\n"
        elif current and line.startswith("- "):
            current["details"].append(line[2:].strip())
            current["raw"] += line[2:] + "\n"
    if current:
        cards.append(current)
    return cards


def get_attribution_summary():
    path = os.path.join(ATTRIB_DIR, "attribution.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            history = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    from collections import defaultdict
    by_agent = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in history:
        for c in t.get("contributors", []):
            agent = c.get("agent", "unknown")
            by_agent[agent]["n"] += 1
            by_agent[agent]["pnl"] += t.get("pnl_pct", 0)
            if t.get("pnl_pct", 0) > 0:
                by_agent[agent]["wins"] += 1

    rows = []
    for agent, s in by_agent.items():
        n = s["n"]
        rows.append({
            "agent": agent,
            "trades": n,
            "avg_pnl": round(s["pnl"] / n, 1) if n else 0,
            "win_rate": round(s["wins"] / n * 100, 1) if n else 0,
        })
    rows.sort(key=lambda r: r["avg_pnl"], reverse=True)
    return rows


def get_agent_memory_snapshot():
    """Return per-agent weight summaries for display."""
    snapshots = {}
    if not os.path.exists(MEMORY_DIR):
        return snapshots
    for name in ["whale", "sentiment", "pattern", "correlation", "news", "dd", "macro", "meta", "advisor",
                 "microcap", "santiment", "derivatives", "fundraising", "kronos"]:
        path = os.path.join(MEMORY_DIR, f"{name}_memory.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                mem = json.load(f)
            weights = mem.get("weights", {})
            agent_stats = mem.get("agent_stats", {})
            regime_weights = mem.get("regime_weights", {})
            snapshots[name] = {
                "runs": mem.get("runs", 0),
                "predictions": len(mem.get("predictions", [])),
                "weights": {k: round(v, 3) for k, v in weights.items()},
                "agent_stats": agent_stats,
                "regime_weights": {
                    r: {k: round(v, 3) for k, v in w.items()}
                    for r, w in regime_weights.items()
                },
            }
        except (OSError, json.JSONDecodeError):
            pass
    return snapshots


def get_signals_summary():
    if not os.path.exists(SIGNALS_FILE):
        return {"count": 0, "by_source": {}, "by_signal": {}}
    try:
        with open(SIGNALS_FILE) as f:
            signals = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"count": 0, "by_source": {}, "by_signal": {}}

    from collections import Counter
    by_source = Counter(s.get("source", "?") for s in signals)
    by_signal = Counter(s.get("signal", "?") for s in signals)
    by_agent = Counter(s.get("agent", "?") for s in signals)

    signal_list = []
    for s in signals[-50:]:
        signal_list.append({
            "coin": s.get("coin"),
            "signal": s.get("signal"),
            "confidence": s.get("confidence"),
            "source": s.get("source"),
            "agent": s.get("agent"),
        })

    return {
        "count": len(signals),
        "by_source": dict(by_source),
        "by_signal": dict(by_signal),
        "by_agent": dict(by_agent),
        "recent": signal_list,
    }


# ── Templates ───────────────────────────────────────────────────────────


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Super Crypto Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Manrope:wght@400;500;600;700&family=Newsreader:opsz,wght@6..72,500;6..72,600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0d0c; --surface: #121716; --surface2: #19201e;
    --border: rgba(225, 235, 229, 0.11); --text: #f2f5f1; --muted: #98a49e;
    --green: #7fc49a; --green-dark: #4c9368; --yellow: #d6b977;
    --yellow-dark: #9f8244; --red: #dd8b83; --red-dark: #b96761;
    --accent: #d5b878; --accent-dark: #b99652; --live: #79c595;
    --font-display: "Newsreader", Georgia, serif;
    --font-body: "Manrope", sans-serif;
    --font-data: "DM Mono", monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: var(--font-body); line-height: 1.65;
    background: radial-gradient(circle at 12% 0%, rgba(92,126,105,.13), transparent 30rem),
                radial-gradient(circle at 95% 18%, rgba(213,184,120,.08), transparent 26rem),
                var(--bg);
    color: var(--text); padding: 16px; max-width: 1120px; margin: 0 auto;
  }
  header { text-align: center; padding: 60px 0 32px; }
  header h1 { font-family: var(--font-display); font-size: clamp(2.8rem, 8vw, 5.3rem); font-weight: 500; line-height: .95; }
  header p { color: var(--muted); font-size: 1rem; margin-top: 16px; }

  .agents-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px; margin: 24px 0;
  }
  .agent-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px; text-align: center; transition: all .2s ease;
  }
  .agent-card:hover { background: var(--surface2); transform: translateY(-3px); }
  .agent-emoji { font-size: 2rem; margin-bottom: 4px; }
  .agent-name { font-weight: 600; font-size: 0.9rem; }
  .agent-status { font-size: 0.7rem; color: var(--muted); margin-top: 4px; }

  .btn {
    display: block; width: 100%; padding: 14px; border: none; border-radius: 12px;
    font-size: 1rem; font-weight: 600; cursor: pointer; margin: 16px 0;
    background: var(--accent); color: #171710;
    font-family: inherit; letter-spacing: .5px;
    transition: transform .25s ease, box-shadow .25s ease;
  }
  .btn:hover { transform: translateY(-2px); box-shadow: 0 16px 36px rgba(213,184,120,.19); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .status-bar {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 16px; background: var(--surface); border-radius: 12px;
    margin-bottom: 16px; font-family: var(--font-data); font-size: .8rem;
  }
  .disclaimer {
    background: rgba(255, 255, 255, .03); border: 1px solid rgba(255, 255, 255, .08);
    border-radius: 10px; padding: 12px 16px; margin: 16px 0;
    font-size: .72rem; line-height: 1.5; color: var(--muted);
  }
  .badge-count {
    background: rgba(213, 184, 120, .12); color: var(--accent);
    padding: 2px 10px; border-radius: 12px; font-size: .75rem;
  }

  .summary-row {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 28px 0;
  }
  .summary-chip {
    padding: 20px 14px; border: 1px solid var(--border); border-radius: 14px;
    background: var(--surface); text-align: center; font-weight: 700; font-size: 1.25rem;
  }
  .summary-chip small { color: var(--muted); font: 500 .62rem/1 var(--font-body); letter-spacing: .12em; }
  .chip-buy { color: var(--green); }
  .chip-watch { color: var(--yellow); }
  .chip-sell { color: var(--red); }
  .chip-avoid { color: var(--red-dark); }

  #cards { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-top: 20px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
    padding: 20px; margin: 0; min-height: 160px;
    transition: all .25s ease; position: relative; overflow: hidden;
  }
  .card:hover {
    background: var(--surface2);
    transform: translateY(-5px);
    box-shadow: 0 24px 48px rgba(0,0,0,.28);
  }
  .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; gap: 12px; }
  .coin-name { font-weight: 700; font-size: 1.1rem; }
  .action-badge {
    padding: 4px 12px; border-radius: 20px; font: 500 .7rem/1 var(--font-data);
    text-transform: uppercase; letter-spacing: .08em; white-space: nowrap;
  }
  .badge-buy { background: rgba(127,196,154,.15); color: var(--green); }
  .badge-watch { background: rgba(214,185,119,.15); color: var(--yellow); }
  .badge-hold { background: rgba(127,160,214,.15); color: #8fb3e8; }
  .badge-sell { background: rgba(221,139,131,.15); color: var(--red); }
  .badge-avoid { background: rgba(221,139,131,.15); color: var(--red-dark); }
  .card-price-row {
    display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; min-height: 22px;
    font: 600 .95rem/1 var(--font-data);
  }
  .card-price-row .px { color: var(--text, #eee); }
  .card-price-row .chg { font-size: .78rem; font-weight: 700; }
  .card-price-row .chg.gain { color: var(--green); }
  .card-price-row .chg.loss { color: var(--red); }
  .card-price-row .chg.flat { color: var(--muted); }
  .card-spark { width: 100%; height: 44px; margin-bottom: 12px; display: block; }
  .card-spark .spark-line { fill: none; stroke-width: 1.75; }
  .card-spark .spark-fill { stroke: none; }
  .card-spark.is-loading { opacity: .25; }
  .card-details { color: var(--muted); font-size: .82rem; }
  .card-filters { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
  .card-details b { color: var(--text); font-weight: 600; }
  .card-details li { margin-bottom: 4px; list-style: none; }
  .card-details li::before { content: "· "; color: var(--accent); }
  .empty { text-align: center; padding: 40px; color: var(--muted); }
  .scan-time { display: inline-block; margin-top: 18px; padding: 6px 14px; border: 1px solid var(--border); background: var(--surface); border-radius: 999px; color: var(--muted); font-size: .85rem; }
  .scan-time strong { color: var(--text); font-weight: 600; }
  .updated { margin-top: 24px; text-align: center; color: var(--muted); font-size: .75rem; }
  h2.section-title { font-size: 1.1rem; margin: 28px 0 14px; }
  .attribution-table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
  .attribution-table th, .attribution-table td {
    padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border);
    font-family: var(--font-data); font-size: .8rem;
  }
  .attribution-table th { color: var(--muted); font-weight: 500; }
  .attribution-table tr:hover { background: rgba(213,184,120,.03); }

  /* Kronos + per-coin agent evidence */
  .sec-sub { font: 400 .72rem var(--font-data); color: var(--muted); margin-left: 8px; }
  .kr-tiles { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 12px; }
  .kr-tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px; }
  .kr-tile .k { font-size: .66rem; color: var(--muted); letter-spacing: .04em; text-transform: uppercase; }
  .kr-tile .v { font: 600 1.25rem var(--font-data); margin-top: 2px; }
  .kr-tile .s { font-size: .68rem; color: var(--muted); }
  .kr-status { font-size: .76rem; color: var(--muted); margin-bottom: 10px; }
  .kr-table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
  .kr-table { width: 100%; border-collapse: collapse; font-size: .8rem; min-width: 560px; }
  .kr-table th, .kr-table td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
  .kr-table th { color: var(--muted); font-weight: 500; font-size: .7rem; text-transform: uppercase; letter-spacing: .04em; }
  .kr-table tr.kr-row { cursor: pointer; }
  .kr-table tr.kr-row:hover { background: var(--surface2); }
  .kr-table td.num { font-family: var(--font-data); }
  .kr-pill { font: 600 .66rem var(--font-data); padding: 2px 7px; border-radius: 99px; border: 1px solid var(--border); color: var(--muted); }
  .kr-pill.up { color: var(--green); border-color: var(--green-dark); }
  .kr-pill.down { color: var(--red); border-color: var(--red-dark); }
  .kr-line { font: 500 .76rem var(--font-data); color: var(--muted); margin: 6px 0 2px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .agent-chips { display: flex; flex-wrap: wrap; gap: 5px; margin: 8px 0 6px; }
  .agent-chip { font-size: .68rem; padding: 2px 7px; border-radius: 99px; background: var(--surface2); border: 1px solid var(--border); color: var(--text); white-space: nowrap; }
  .agent-chip.bear { border-color: var(--red-dark); }
  .agent-chip.kr { border-color: var(--accent-dark); }
  .intel-sec { margin-top: 18px; border-top: 1px solid var(--border); padding-top: 14px; }
  .intel-title { font-size: .85rem; font-weight: 600; margin-bottom: 8px; }
  .ev-agent { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; }
  .ev-head { display: flex; justify-content: space-between; gap: 8px; font-size: .8rem; font-weight: 600; }
  .ev-sig { font: 400 .74rem var(--font-data); color: var(--muted); margin-top: 4px; }
  .ev-sig .why { color: var(--text); font-family: var(--font-body); }
  .hl-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; font-size: .82rem; color: var(--muted); }
  .hl-pill { font: 700 .72rem var(--font-data); padding: 3px 10px; border-radius: 99px; letter-spacing: .05em; }
  .hl-ok { background: rgba(127,196,154,.15); color: var(--green); }
  .hl-warn { background: rgba(214,185,119,.15); color: var(--yellow); }
  .hl-fail { background: rgba(221,139,131,.18); color: var(--red); }
  .hl-list { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
  .hl-row { display: grid; grid-template-columns: 14px minmax(140px, 220px) 1fr; gap: 10px; padding: 9px 14px; border-bottom: 1px solid var(--border); font-size: .8rem; align-items: baseline; }
  .hl-row:last-child { border-bottom: none; }
  .hl-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .hl-row .d { color: var(--muted); overflow-wrap: anywhere; }
  .hl-hist { display: flex; gap: 2px; align-items: center; }
  .hl-hist i { width: 6px; height: 14px; border-radius: 2px; display: inline-block; }
  @media (max-width: 560px) { .hl-row { grid-template-columns: 14px 1fr; } .hl-row .d { grid-column: 2; } }
  .pf-status { font-size: .76rem; color: var(--muted); margin: 10px 0; }
  .pf-sub { font-size: .8rem; font-weight: 600; margin: 16px 0 8px; }
  .pf-bar { position: relative; height: 6px; border-radius: 99px; background: var(--surface2); border: 1px solid var(--border); min-width: 90px; }
  .pf-bar i { position: absolute; top: -3px; width: 2px; height: 10px; background: var(--text); border-radius: 2px; }
  .pf-curve { width: 100%; height: 70px; display: block; margin-top: 4px; }
  .kr-chart { width: 100%; height: auto; display: block; background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; }
  .kr-grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 10px; }
  @media (max-width: 700px) {
    .kr-tiles { grid-template-columns: repeat(2, 1fr); }
    .kr-grid4 { grid-template-columns: repeat(2, 1fr); }
  }

  @media (max-width: 700px) {
    body { padding: 8px; }
    header { padding: 40px 0 24px; }
    .summary-row { grid-template-columns: repeat(2, 1fr); }
    #cards { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<div class="disclaimer">
  ⚠️ Disclaimer: This tool provides market intelligence signals for informational and educational purposes only. It is a personal research tool — not financial advice. Cryptocurrency markets are highly volatile; you may lose some or all of your invested capital. Past performance does not indicate future results. You are solely responsible for your investment decisions.
</div>

<header>
  <h1>Super Crypto Agent</h1>
  <p>⚡ Auto-scanning crypto markets with {{ agent_count }} specialized agents — BUY / WATCH / AVOID verdicts updated live</p>
  <div class="scan-time" id="scanTime">🕒 Last scan: <strong>—</strong></div>
</header>

<div class="status-bar">
  <span>📡 Signal bus active</span>
  <span class="badge-count" id="signalCount">0 signals</span>
</div>

<button class="btn" id="runBtn" onclick="startScan()">▶ Run Full Scan</button>

<div id="scanStatus" style="text-align:center;color:var(--muted);font-size:0.82rem;margin-bottom:16px">Idle</div>

<h2 class="section-title">🩺 System Health <span class="sec-sub">automatic checks after every scan</span></h2>
<div id="healthPanel"><div class="empty">Loading health checks…</div></div>

<h2 class="section-title">🤖 Agent Team ({{ agent_count }} agents)</h2>
<div class="agents-grid" id="agentGrid"></div>

<h2 class="section-title">🔮 Kronos Forecasts <span class="sec-sub">next 24h · foundation model on 4h candles</span></h2>
<div id="kronosPanel"><div class="empty">Loading Kronos forecasts…</div></div>

<h2 class="section-title">📊 Verdict Summary</h2>
<div class="summary-row" id="summaryRow" style="display:none">
  <div class="summary-chip chip-buy" id="buyCount" style="cursor:pointer" onclick="setCardFilter('BUY');document.getElementById('cardFilters').scrollIntoView({behavior:'smooth'})">0<br><small>BUY</small></div>
  <div class="summary-chip chip-watch" id="watchCount" style="cursor:pointer" onclick="setCardFilter('WATCH');document.getElementById('cardFilters').scrollIntoView({behavior:'smooth'})">0<br><small>WATCH</small></div>
  <div class="summary-chip chip-sell" id="sellCount">0<br><small>SELL</small></div>
  <div class="summary-chip chip-avoid" id="avoidCount" style="cursor:pointer" onclick="setCardFilter('AVOID');document.getElementById('cardFilters').scrollIntoView({behavior:'smooth'})">0<br><small>AVOID</small></div>
</div>

<h2 class="section-title">🪙 Verdicts</h2>
<div class="card-filters" id="cardFilters"></div>
<div id="cards"><div class="empty">No report yet — run a scan to get started</div></div>
<div id="cardsMore"></div>

<h2 class="section-title">💼 Paper Portfolio <span class="sec-sub">simulated trades from BUY verdicts · not real money</span></h2>
<div id="portfolioPanel"><div class="empty">Loading paper portfolio…</div></div>

<h2 class="section-title">📈 Agent Attribution (P&L by agent)</h2>
<div id="attributionTable"></div>

<div class="updated" id="updated"></div>

<script>
const AGENTS = [
  {name: "Micro-Cap Finder", emoji: "🔬", key: "microcap"},
  {name: "Whale Detector", emoji: "⚡", key: "whale"},
  {name: "News Scanner", emoji: "📰", key: "news"},
  {name: "Sentiment", emoji: "💭", key: "sentiment"},
  {name: "Pattern Recognition", emoji: "📊", key: "pattern"},
  {name: "Correlation Monitor", emoji: "🔗", key: "correlation"},
  {name: "Due Diligence", emoji: "🛡️", key: "dd"},
  {name: "On-Chain Holders", emoji: "⛓️", key: "onchain"},
  {name: "Macro Regime", emoji: "🌐", key: "macro"},
  {name: "Meta-Learner", emoji: "🧠", key: "meta"},
  {name: "Santiment Activity", emoji: "🛰️", key: "santiment"},
  {name: "Derivatives Flow", emoji: "📉", key: "derivatives"},
  {name: "Fresh Funding", emoji: "💰", key: "fundraising"},
  {name: "Kronos Forecast", emoji: "🔮", key: "kronos"},
];
const AGENT_META = Object.fromEntries(AGENTS.map(a => [a.key, a]));
AGENT_META.advisor = {name: "Advisor", emoji: "📋", key: "advisor"};
const CARD_DATA = {};
let ALL_CARDS = [], CARD_FILTER = 'ALL', CARD_LIMIT = 12;
const HYDRATED = new Set();

function mdBold(t) {
  // report lines carry **bold** markdown; render it instead of showing asterisks
  return String(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
}

function setCardFilter(f) { CARD_FILTER = f; CARD_LIMIT = 12; renderCards(); }
function showMoreCards() { CARD_LIMIT += 12; renderCards(); }

function renderCards() {
  const counts = {ALL: ALL_CARDS.length};
  ALL_CARDS.forEach(c => { counts[c.action] = (counts[c.action] || 0) + 1; });
  const tabs = ['ALL', 'BUY', 'WATCH', 'HOLD', 'AVOID'].filter(k => k === 'ALL' || counts[k]);
  document.getElementById('cardFilters').innerHTML = tabs.map(k =>
    '<button class="tf-btn' + (CARD_FILTER === k ? ' active' : '') + '" onclick="setCardFilter(\'' + k + '\')">' +
    (k === 'ALL' ? 'All' : k) + ' <span style="opacity:.7">' + (counts[k] || 0) + '</span></button>').join('');
  const list = ALL_CARDS.filter(c => CARD_FILTER === 'ALL' || c.action === CARD_FILTER);
  const shown = list.slice(0, CARD_LIMIT);
  let html = '';
  shown.forEach(c => {
    const badgeClass = c.action === 'BUY' ? 'badge-buy' :
                       c.action === 'HOLD' ? 'badge-hold' :
                       c.action === 'WATCH' ? 'badge-watch' :
                       c.action === 'SELL' ? 'badge-sell' : 'badge-avoid';
    const riskLine = (c.risk_notes || []).length
      ? '<div class="kr-line">🛡️ ' + (c.risk_notes || []).map(esc).join(' · ') + '</div>' : '';
    const details = (c.details || []).map(d => '<li>' + mdBold(d) + '</li>').join('');
    const sym = c.coin;
    html += '<div class="card" onclick="showCoin(\'' + sym + '\', \'1d\')">' +
        '<div class="card-header">' +
          '<span class="coin-name">' + sym + '</span>' +
          '<span class="action-badge ' + badgeClass + '">' + c.action + '</span>' +
        '</div>' +
        '<div class="card-price-row" id="price-' + sym + '"></div>' +
        '<svg class="card-spark is-loading" id="spark-' + sym + '" viewBox="0 0 100 32" preserveAspectRatio="none"></svg>' +
        kronosLine(c.kronos) +
        riskLine +
        agentChips(c.evidence) +
        '<ul class="card-details">' + details + '</ul>' +
      '</div>';
  });
  document.getElementById('cards').innerHTML = html || '<div class="empty">No ' + (CARD_FILTER === 'ALL' ? '' : CARD_FILTER + ' ') + 'verdicts</div>';
  const rest = list.length - shown.length;
  document.getElementById('cardsMore').innerHTML = rest > 0
    ? '<button class="btn" style="margin-top:14px" onclick="showMoreCards()">Show ' + Math.min(rest, 12) + ' more (' + rest + ' hidden)</button>'
    : '';
  // Price + sparkline hydrate progressively after the list is visible; cache
  // the fetched markup so re-filtering doesn't refetch.
  shown.forEach(c => {
    if (HYDRATED.has(c.coin)) {
      const h = HYDRATED_HTML[c.coin];
      if (h) {
        document.getElementById('price-' + c.coin).innerHTML = h.price;
        const sp = document.getElementById('spark-' + c.coin);
        sp.innerHTML = h.spark; sp.classList.remove('is-loading');
      }
      return;
    }
    HYDRATED.add(c.coin);
    hydrateCardChart(c.coin).then(() => {
      const pe = document.getElementById('price-' + c.coin), se = document.getElementById('spark-' + c.coin);
      HYDRATED_HTML[c.coin] = {price: pe ? pe.innerHTML : '', spark: se ? se.innerHTML : ''};
    });
  });
}
const HYDRATED_HTML = {};
let KRONOS = null;

function renderAgents() {
  const grid = document.getElementById("agentGrid");
  grid.innerHTML = AGENTS.map(a => `
    <div class="agent-card">
      <div class="agent-emoji">${a.emoji}</div>
      <div class="agent-name">${a.name}</div>
      <div class="agent-status">Active</div>
    </div>
  `).join('');
}
renderAgents();

async function startScan() {
  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  btn.textContent = '▶ Scanning...';
  document.getElementById('scanStatus').textContent = 'Pipeline running...';

  try {
    await fetch('/api/scan', { method: 'POST' });
    pollStatus();
  } catch(e) {
    btn.disabled = false;
    btn.textContent = '▶ Run Full Scan';
    document.getElementById('scanStatus').textContent = 'Error starting scan';
  }
}

function pollStatus() {
  let timer = setInterval(async () => {
    try {
      const r = await fetch('/api/status');
      const s = await r.json();
      if (!s.running) {
        clearInterval(timer);
        document.getElementById('runBtn').disabled = false;
        document.getElementById('runBtn').textContent = '▶ Run Full Scan';
        document.getElementById('scanStatus').textContent = 'Scan complete!';
        loadReport();
      } else {
        document.getElementById('scanStatus').textContent = 'Pipeline running...';
      }
    } catch(e) {}
  }, 3000);
}

// Every scan time on the page is shown in Japan Standard Time, regardless of
// the viewer's browser timezone or the server's (UTC) clock.
function formatJST(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    }).formatToParts(d).map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} JST`;
}

async function loadReport() {
    try {
        const r = await fetch('/api/report');
        const data = await r.json();
        if (!data.report) {
            document.getElementById('cards').innerHTML = '<div class="empty">No report yet â run a scan to get started</div>';
            return;
        }
        const scanJst = formatJST(data.timestamp);
        document.getElementById('updated').textContent = 'Last scan: ' + scanJst;
        document.getElementById('scanTime').innerHTML = '🕒 Last scan: <strong>' + scanJst + '</strong>';

        const cards = data.cards || [];
        let buy=0, watch=0, sell=0, avoid=0;
        cards.forEach(c => {
            if (c.action==='BUY') buy++;
            else if (c.action==='WATCH') watch++;
            else if (c.action==='SELL') sell++;
            else if (c.action==='AVOID') avoid++;
        });

        if (cards.length) {
            document.getElementById('summaryRow').style.display = 'grid';
            document.getElementById('buyCount').innerHTML = buy + '<br><small>BUY</small>';
            document.getElementById('watchCount').innerHTML = watch + '<br><small>WATCH</small>';
            document.getElementById('sellCount').innerHTML = sell + '<br><small>SELL</small>';
            document.getElementById('avoidCount').innerHTML = avoid + '<br><small>AVOID</small>';
        }

        let html = '';
        ALL_CARDS = cards;
        cards.forEach(c => { CARD_DATA[c.coin] = c; });
        CARD_LIMIT = 12;
        renderCards();
    } catch(e) {
        document.getElementById('cards').innerHTML = '<div class="empty">Failed to load report</div>';
    }
}

async function loadSignals() {
  try {
    const r = await fetch('/api/signals');
    const data = await r.json();
    document.getElementById('signalCount').textContent = data.count + ' signals';
  } catch(e) {}
}

async function loadAttribution() {
  try {
    const r = await fetch('/api/attribution');
    const data = await r.json();
    const rows = data.agents || [];
    if (rows.length === 0) {
      document.getElementById('attributionTable').innerHTML = '<p style="color:var(--muted)">No attribution data yet — run a scan with paper trading</p>';
      return;
    }
    let html = '<table class="attribution-table"><thead><tr>' +
      '<th>Agent</th><th>Trades</th><th>Avg P&L%</th><th>Win Rate</th></tr></thead><tbody>';
    rows.forEach(a => {
      html += `<tr><td>${a.agent}</td><td>${a.trades}</td>` +
              `<td class="${a.avg_pnl > 0 ? 'gain' : a.avg_pnl < 0 ? 'loss' : 'flat'}">${a.avg_pnl}</td>` +
              `<td>${a.win_rate}%</td></tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('attributionTable').innerHTML = html;
  } catch(e) {}
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function pctCls(v) { return v > 0 ? 'gain' : v < 0 ? 'loss' : 'flat'; }
function fmtSigned(v, d) {
  if (v == null || !Number.isFinite(v)) return '—';
  return (v > 0 ? '+' : '') + v.toFixed(d == null ? 1 : d) + '%';
}
function timeAgo(iso) {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return '';
  const h = ms / 3.6e6;
  if (h < 1) return Math.max(1, Math.round(ms / 6e4)) + 'm ago';
  if (h < 48) return Math.round(h) + 'h ago';
  return Math.round(h / 24) + 'd ago';
}
function groupEvidence(ev) {
  const by = {};
  (ev || []).forEach(e => { (by[e.agent] = by[e.agent] || []).push(e); });
  return by;
}
function kronosPill(fc) {
  if (!fc || !fc.signal) return '<span class="kr-pill">no call</span>';
  const up = fc.signal === 'kronos_forecast_up';
  return '<span class="kr-pill ' + (up ? 'up' : 'down') + '">' + (up ? '▲ UP' : '▼ DOWN') + ' signal</span>';
}
function kronosLine(fc) {
  if (!fc) return '';
  const agree = Math.round((fc.path_agreement || 0) * (fc.paths || 0));
  return '<div class="kr-line">🔮 Kronos 24h <b class="' + pctCls(fc.expected_move_pct) + '">' +
    fmtSigned(fc.expected_move_pct) + '</b> · ' + agree + '/' + (fc.paths || 0) + ' paths ' + kronosPill(fc) + '</div>';
}
function agentChips(ev) {
  const by = groupEvidence(ev);
  const keys = Object.keys(by);
  if (!keys.length) return '';
  return '<div class="agent-chips" title="Agents with signals on this coin">' + keys.map(k => {
    const m = AGENT_META[k] || {name: k, emoji: '•'};
    const bear = by[k].every(e => e.bearish);
    return '<span class="agent-chip' + (k === 'kronos' ? ' kr' : '') + (bear ? ' bear' : '') + '" title="' +
      esc(m.name + ': ' + by[k].map(e => e.signal).join(', ')) + '">' + m.emoji + ' ' + esc(m.name) +
      (by[k].length > 1 ? ' ×' + by[k].length : '') + '</span>';
  }).join('') + '</div>';
}

// Kronos fan chart: last 7 days of 4h closes, then the forecast median with
// its 10-90% path band, split by a "now" line.
function kronosFanSVG(fc, w, h, mini) {
  const hist = (fc.history || []).map(p => p.c);
  const f = fc.forecast || [];
  if (hist.length < 2 || !f.length) return '';
  const all = hist.concat(f.map(p => p.p10), f.map(p => p.p90), [fc.reference_price]).filter(Number.isFinite);
  const min = Math.min(...all), max = Math.max(...all), span = (max - min) || 1;
  const n = hist.length + f.length;
  const padL = mini ? 1 : 6, padR = mini ? 1 : 64, padY = mini ? 2 : 14;
  const X = i => padL + i * (w - padL - padR) / (n - 1);
  const Y = v => padY + (1 - (v - min) / span) * (h - padY * 2);
  const last = hist.length - 1;
  const histPath = hist.map((v, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join(' ');
  const med = [[last, fc.reference_price || hist[last]]].concat(f.map((p, i) => [last + 1 + i, p.median]));
  const medPath = med.map((pt, i) => (i ? 'L' : 'M') + X(pt[0]).toFixed(1) + ',' + Y(pt[1]).toFixed(1)).join(' ');
  const top = [[last, fc.reference_price || hist[last]]].concat(f.map((p, i) => [last + 1 + i, p.p90]));
  const bot = f.map((p, i) => [last + 1 + i, p.p10]).reverse();
  const band = top.concat(bot).map((pt, i) => (i ? 'L' : 'M') + X(pt[0]).toFixed(1) + ',' + Y(pt[1]).toFixed(1)).join(' ') + ' Z';
  const col = (fc.expected_move_pct || 0) >= 0 ? 'var(--green)' : 'var(--red)';
  let svg = '<svg class="' + (mini ? '' : 'kr-chart') + '" viewBox="0 0 ' + w + ' ' + h + '" ' +
    (mini ? 'width="' + w + '" height="' + h + '"' : 'preserveAspectRatio="none"') + '>' +
    '<path d="' + band + '" fill="' + col + '" fill-opacity="0.16" stroke="none"/>' +
    '<path d="' + histPath + '" fill="none" stroke="var(--muted)" stroke-width="' + (mini ? 1.2 : 1.6) + '"/>' +
    '<path d="' + medPath + '" fill="none" stroke="' + col + '" stroke-width="' + (mini ? 1.5 : 2) + '" stroke-dasharray="' + (mini ? '0' : '5 3') + '"/>';
  if (!mini) {
    const nx = X(last).toFixed(1);
    const endV = f[f.length - 1].median;
    svg += '<line x1="' + nx + '" x2="' + nx + '" y1="4" y2="' + (h - 4) + '" stroke="var(--border)" stroke-dasharray="2 3"/>' +
      '<text x="' + (X(last) - 4).toFixed(1) + '" y="12" fill="var(--muted)" font-size="10" text-anchor="end" font-family="DM Mono, monospace">now</text>' +
      '<text x="' + (w - padR + 6) + '" y="' + (Y(endV) + 4).toFixed(1) + '" fill="' + col + '" font-size="11" font-family="DM Mono, monospace">' + fmtPrice(endV) + '</text>' +
      '<text x="' + (w - padR + 6) + '" y="' + (Y(max) + 8).toFixed(1) + '" fill="var(--muted)" font-size="10" font-family="DM Mono, monospace">' + fmtPrice(max) + '</text>' +
      '<text x="' + (w - padR + 6) + '" y="' + (Y(min)).toFixed(1) + '" fill="var(--muted)" font-size="10" font-family="DM Mono, monospace">' + fmtPrice(min) + '</text>';
  }
  return svg + '</svg>';
}

function trTile(k, v, s) {
  return '<div class="kr-tile"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="s">' + (s || '') + '</div></div>';
}

async function loadKronos() {
  const el = document.getElementById('kronosPanel');
  try {
    const r = await fetch('/api/kronos');
    KRONOS = await r.json();
  } catch (e) {
    el.innerHTML = '<div class="empty">Kronos data unavailable</div>';
    return;
  }
  const st = KRONOS.stats || {}, tr = KRONOS.track_record || {};
  const sig = tr.signals || {}, all = tr.all || {};
  const pct = v => v == null ? '—' : v + '%';
  let html = '<div class="kr-tiles">' +
    trTile('Signal hit rate', pct(sig.hit_rate), (sig.n || 0) + ' graded signals') +
    trTile('All-forecast hit rate', pct(all.hit_rate), (all.n || 0) + ' graded forecasts') +
    trTile('Inside 10–90% band', pct(all.in_range), 'calibration check') +
    trTile('Avg abs. error', all.mae == null ? '—' : all.mae + '%', 'forecast vs actual 24h') +
    trTile('Following calls', all.avg_return == null ? '—' : fmtSigned(all.avg_return, 2), 'avg 24h return, ' + (tr.pending || 0) + ' pending') +
    '</div>';
  if (st.unavailable) {
    html += '<div class="kr-status">⚠️ Kronos sat out the last scan: ' + esc(st.unavailable) + '</div>';
  } else if (st.at) {
    html += '<div class="kr-status">Last run ' + formatJST(st.at) + ' · ' + (st.forecast || 0) + ' coins forecast · ' +
      (st.signals || 0) + ' strong enough to signal · ' + esc(st.model || '') + ' · ' + (st.seconds || 0) + 's of inference</div>';
  }
  const rows = Object.entries(KRONOS.latest || {}).sort((a, b) =>
    (b[1].signal ? 1 : 0) - (a[1].signal ? 1 : 0) || Math.abs(b[1].expected_move_pct) - Math.abs(a[1].expected_move_pct));
  if (!rows.length) {
    el.innerHTML = html + '<div class="empty">No forecasts yet — they appear after the next scheduled scan.</div>';
    return;
  }
  const byCoin = tr.by_coin || {};
  html += '<div class="kr-table-wrap"><table class="kr-table"><thead><tr><th>Coin</th><th>Path</th><th>Expected 24h</th>' +
    '<th>10–90% range</th><th>Agreement</th><th>Call</th><th>Track record</th></tr></thead><tbody>';
  rows.forEach(([coin, fc]) => {
    const rec = byCoin[coin];
    html += '<tr class="kr-row" onclick="showCoin(\'' + esc(coin) + '\', \'1d\')">' +
      '<td><b>' + esc(coin) + '</b></td>' +
      '<td>' + kronosFanSVG(fc, 90, 26, true) + '</td>' +
      '<td class="num ' + pctCls(fc.expected_move_pct) + '">' + fmtSigned(fc.expected_move_pct, 2) + '</td>' +
      '<td class="num">' + fmtSigned(fc.range_pct[0]) + ' … ' + fmtSigned(fc.range_pct[1]) + '</td>' +
      '<td class="num">' + Math.round((fc.path_agreement || 0) * 100) + '%</td>' +
      '<td>' + kronosPill(fc) + '</td>' +
      '<td class="num">' + (rec && rec.n ? rec.hit_rate + '% of ' + rec.n : '—') + '</td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

function renderCoinIntel(symbol) {
  const kEl = document.getElementById('modalKronos');
  const aEl = document.getElementById('modalAgents');
  const fc = (KRONOS && KRONOS.latest && KRONOS.latest[symbol]) || (CARD_DATA[symbol] && CARD_DATA[symbol].kronos);
  if (fc) {
    const rec = ((KRONOS && KRONOS.track_record && KRONOS.track_record.by_coin) || {})[symbol];
    const agree = Math.round((fc.path_agreement || 0) * (fc.paths || 0));
    kEl.innerHTML = '<div class="intel-sec"><div class="intel-title">🔮 Kronos forecast · next ' + (fc.horizon_h || 24) + 'h ' + kronosPill(fc) + '</div>' +
      kronosFanSVG(fc, 640, 190, false) +
      '<div class="kr-grid4">' +
        '<div class="stat-item"><div class="stat-label">Expected move</div><div class="stat-value ' + pctCls(fc.expected_move_pct) + '">' + fmtSigned(fc.expected_move_pct, 2) + '</div></div>' +
        '<div class="stat-item"><div class="stat-label">10–90% range</div><div class="stat-value">' + fmtSigned(fc.range_pct[0]) + ' … ' + fmtSigned(fc.range_pct[1]) + '</div></div>' +
        '<div class="stat-item"><div class="stat-label">Paths agreeing</div><div class="stat-value">' + agree + ' / ' + (fc.paths || 0) + '</div></div>' +
        '<div class="stat-item"><div class="stat-label">Move vs daily vol</div><div class="stat-value">' + (fc.move_vs_vol == null ? '—' : fc.move_vs_vol + '×') + '</div></div>' +
      '</div>' +
      '<div style="font-size:.72rem;color:var(--muted);margin-top:8px">Target ' + formatJST(fc.target_time) + ' · ref price ' + fmtPrice(fc.reference_price) +
      ' · t-stat ' + (fc.t_stat == null ? '—' : fc.t_stat) + ' · ' + (fc.context_bars || 0) + ' bars of context · ' +
      (rec && rec.n ? 'Kronos on ' + esc(symbol) + ': ' + rec.hit_rate + '% direction hits over ' + rec.n + ' graded forecasts' : 'no graded forecasts for this coin yet') +
      '</div></div>';
  } else {
    kEl.innerHTML = '';
  }
  const c = CARD_DATA[symbol];
  const by = groupEvidence(c && c.evidence);
  const keys = Object.keys(by);
  if (!keys.length) { aEl.innerHTML = ''; return; }
  aEl.innerHTML = '<div class="intel-sec"><div class="intel-title">🤖 Agents working on ' + esc(symbol) + ' (' + keys.length + ')</div>' +
    keys.map(k => {
      const m = AGENT_META[k] || {name: k, emoji: '•'};
      return '<div class="ev-agent"><div class="ev-head"><span>' + m.emoji + ' ' + esc(m.name) + '</span><span style="color:var(--muted);font-weight:400;font-size:.72rem">' +
        by[k].length + ' signal' + (by[k].length > 1 ? 's' : '') + '</span></div>' +
        by[k].map(e => '<div class="ev-sig"><span class="' + (e.bearish ? 'loss' : 'gain') + '">' + esc(e.signal) + '</span> · conf ' +
          (e.confidence == null ? '—' : Number(e.confidence).toFixed(2)) + ' · ' + timeAgo(e.timestamp) +
          (e.reason ? '<br><span class="why">' + esc(e.reason) + '</span>' : '') + '</div>').join('') +
        '</div>';
    }).join('') + '</div>';
}

loadReport();
// ── Paper portfolio ──
function pfPrice(v) {
  if (v == null || !Number.isFinite(v)) return '—';
  if (v >= 1000) return '$' + v.toLocaleString(undefined, {maximumFractionDigits: 0});
  if (v >= 1) return '$' + v.toFixed(3);
  return '$' + v.toPrecision(4);
}
function equityCurveSVG(curve, start) {
  const pts = (curve || []).map(p => p.equity).filter(Number.isFinite);
  if (pts.length < 2) return '';
  const all = pts.concat([start]);
  let min = Math.min(...all), max = Math.max(...all);
  if (max - min < start * 0.002) { min = start * 0.995; max = start * 1.005; }  // flat: centre it
  const span = max - min;
  const w = 600, h = 70, pad = 4;
  const X = i => pad + i * (w - pad * 2) / (pts.length - 1);
  const Y = v => pad + (1 - (v - min) / span) * (h - pad * 2);
  const path = pts.map((v, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join(' ');
  const col = pts[pts.length - 1] >= start ? 'var(--green)' : 'var(--red)';
  return '<svg class="pf-curve" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
    '<line x1="0" x2="' + w + '" y1="' + Y(start).toFixed(1) + '" y2="' + Y(start).toFixed(1) + '" stroke="var(--border)" stroke-dasharray="3 3"/>' +
    '<path d="' + path + '" fill="none" stroke="' + col + '" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>';
}
// Where the price sits between stop (left) and target (right).
function stopTargetBar(p) {
  if (!p.stop_price || !p.target_price) return '—';
  const pos = Math.max(0, Math.min(1, (p.last_price - p.stop_price) / (p.target_price - p.stop_price)));
  const entryPos = (p.entry_price - p.stop_price) / (p.target_price - p.stop_price);
  return '<div class="pf-bar" title="stop ' + pfPrice(p.stop_price) + ' · target ' + pfPrice(p.target_price) + '">' +
    '<i style="left:' + (entryPos * 100).toFixed(1) + '%;background:var(--muted)"></i>' +
    '<i style="left:' + (pos * 100).toFixed(1) + '%;background:' + (p.last_price >= p.entry_price ? 'var(--green)' : 'var(--red)') + '"></i></div>';
}
async function loadPortfolio() {
  const el = document.getElementById('portfolioPanel');
  let pf;
  try { pf = await (await fetch('/api/portfolio')).json(); } catch (e) { pf = null; }
  if (!pf || !pf.available) { el.innerHTML = '<div class="empty">No paper portfolio yet</div>'; return; }
  const m = pf.metrics || {};
  const invested = pf.positions.reduce((a, p) => a + p.value, 0);
  const openPnl = pf.positions.reduce((a, p) => a + (p.value - p.cost), 0);
  let html = '<div class="kr-tiles">' +
    trTile('Equity', (pf.equity || 0).toFixed(2), 'started at ' + pf.starting_equity) +
    trTile('Total return', '<span class="' + pctCls(pf.return_pct) + '">' + fmtSigned(pf.return_pct, 2) + '</span>', 'max drawdown ' + (m.max_drawdown_pct || 0) + '%') +
    trTile('Open P&L', '<span class="' + pctCls(openPnl) + '">' + (openPnl >= 0 ? '+' : '') + openPnl.toFixed(2) + '</span>', pf.positions.length + ' open · ' + invested.toFixed(1) + ' invested') +
    trTile('Cash', pf.cash.toFixed(2), 'available for new buys') +
    trTile('Closed trades', String(m.closed_trades || 0), m.win_rate == null ? 'no win rate yet' : m.win_rate + '% winners') +
    '</div>' + equityCurveSVG(pf.equity_curve, pf.starting_equity) +
    '<div class="pf-status">Prices marked at the last scan; live prices fill in below when available. Stops at −15%, targets at +45%, checked every scan.</div>';
  if (pf.positions.length) {
    html += '<div class="pf-sub">Open positions</div><div class="kr-table-wrap"><table class="kr-table"><thead><tr>' +
      '<th>Coin</th><th>Opened</th><th>Entry</th><th>Now</th><th>P&L</th><th>Size</th><th>Stop ← → Target</th></tr></thead><tbody>';
    pf.positions.forEach(p => {
      html += '<tr class="kr-row" onclick="showCoin(\'' + esc(p.coin) + '\', \'1d\')">' +
        '<td><b>' + esc(p.coin) + '</b></td>' +
        '<td class="num">' + timeAgo(p.opened_at) + '</td>' +
        '<td class="num">' + pfPrice(p.entry_price) + '</td>' +
        '<td class="num" id="pf-now-' + esc(p.coin) + '">' + pfPrice(p.last_price) + '</td>' +
        '<td class="num ' + pctCls(p.pnl_pct) + '" id="pf-pnl-' + esc(p.coin) + '">' + fmtSigned(p.pnl_pct, 2) + '</td>' +
        '<td class="num">' + p.size_pct + '%</td>' +
        '<td>' + stopTargetBar(p) + '</td></tr>';
    });
    html += '</tbody></table></div>';
  } else {
    html += '<div class="empty">No open positions — waiting for BUY verdicts.</div>';
  }
  if (pf.closed.length) {
    html += '<div class="pf-sub">Closed trades</div><div class="kr-table-wrap"><table class="kr-table"><thead><tr>' +
      '<th>Coin</th><th>Closed</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th></tr></thead><tbody>';
    pf.closed.forEach(t => {
      html += '<tr><td><b>' + esc(t.coin) + '</b></td><td class="num">' + timeAgo(t.closed_at) + '</td>' +
        '<td class="num">' + pfPrice(t.entry_price) + '</td><td class="num">' + pfPrice(t.exit_price) + '</td>' +
        '<td class="num ' + pctCls(t.pnl_pct) + '">' + fmtSigned(t.pnl_pct, 2) + '</td>' +
        '<td>' + esc(String(t.exit_reason || '').replace('_', ' ')) + '</td></tr>';
    });
    html += '</tbody></table></div>';
  }
  el.innerHTML = html;
  // Live mark: reuse the coin-price endpoint the verdict cards already use.
  pf.positions.forEach(async p => {
    const d = await loadCoinData(p.coin);
    if (!d || !Number.isFinite(d.price) || !p.entry_price) return;
    const pnl = (d.price - p.entry_price) / p.entry_price * 100;
    const now = document.getElementById('pf-now-' + p.coin), pe = document.getElementById('pf-pnl-' + p.coin);
    if (now) now.innerHTML = pfPrice(d.price) + ' <span class="kr-pill">live</span>';
    if (pe) { pe.textContent = fmtSigned(pnl, 2); pe.className = 'num ' + pctCls(pnl); }
  });
}

// ── System health ──
const HL_COL = {ok: 'var(--green)', warn: 'var(--yellow)', fail: 'var(--red)'};
async function loadHealth() {
  const el = document.getElementById('healthPanel');
  let h;
  try { h = await (await fetch('/api/healthcheck')).json(); } catch (e) { h = null; }
  if (!h || !h.checks || !h.checks.length) {
    el.innerHTML = '<div class="empty">No health report yet — it appears after the next scan.</div>';
    return;
  }
  const checks = h.checks.slice();
  // Checked in the browser too: a scan that never ran can't report itself.
  const ageH = (Date.now() - new Date(h.generated_at).getTime()) / 3.6e6;
  if (ageH > 7) checks.unshift({name: 'Scans running', status: 'fail',
    detail: 'last scan finished ' + Math.round(ageH) + 'h ago (scans run every 6h)'});
  const rank = {ok: 0, warn: 1, fail: 2};
  const overall = checks.reduce((a, c) => rank[c.status] > rank[a] ? c.status : a, 'ok');
  const label = {ok: 'ALL GOOD', warn: 'WARNING', fail: 'PROBLEM'}[overall];
  const hist = (h.history || []).slice(-30).map(x =>
    '<i title="' + esc(formatJST(x.t) + ': ' + x.overall) + '" style="background:' + (HL_COL[x.overall] || 'var(--border)') + '"></i>').join('');
  checks.sort((a, b) => rank[b.status] - rank[a.status]);
  el.innerHTML = '<div class="hl-head"><span class="hl-pill hl-' + overall + '">' + label + '</span>' +
    '<span>checked ' + formatJST(h.generated_at) + (h.code_version ? ' · code ' + esc(h.code_version) : '') + '</span>' +
    (hist ? '<span class="hl-hist" title="last scans, oldest → newest">' + hist + '</span>' : '') +
    (overall === 'fail' ? '<span>· a GitHub Issue (label <b>health-alert</b>) is opened automatically</span>' : '') +
    '</div><div class="hl-list">' + checks.map(c =>
      '<div class="hl-row"><span class="hl-dot" style="background:' + HL_COL[c.status] + '"></span>' +
      '<b>' + esc(c.name) + '</b><span class="d">' + esc(c.detail) + '</span></div>').join('') + '</div>';
}

loadHealth();
loadKronos();
loadPortfolio();
loadSignals();
loadAttribution();
</script>

<!-- Coin Detail Modal with Chart -->
<div class="modal-overlay" id="coinModal" hidden>
  <div class="modal-content" id="coinModalContent">
    <div class="modal-header">
      <h3 id="modalCoinName">Coin</h3>
      <button class="modal-close" id="modalClose">&times;</button>
    </div>
    <div class="modal-body">
      <div class="market-stats" id="modalStats"></div>
      <div class="timeframes" id="modalTimeframes"></div>
      <label class="vol-toggle"><input type="checkbox" id="volToggle" checked> Vol</label>
      <div class="chart-wrap">
        <canvas id="coinChart" width="800" height="400"></canvas>
      </div>
      <div class="ohlc-readout" id="ohlcReadout"></div>
      <div class="ohlc-stats-grid" id="ohlcStatsGrid"></div>
      <div id="chartNote" style="font-size:.7rem;color:var(--muted);margin-top:8px;text-align:center"></div>
      <div id="modalKronos"></div>
      <div id="modalAgents"></div>
    </div>
  </div>
</div>

<style>
  .modal-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.85); display: none; align-items: center;
    justify-content: center; z-index: 1000; padding: 20px;
  }
  .modal-overlay:not([hidden]) { display: flex; }
  .modal-content {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 16px; max-width: 800px; width: 100%; max-height: 85vh;
    overflow-y: auto; position: relative;
  }
  .modal-header {
    padding: 16px 20px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
  }
  .modal-header h3 { font-size: 1.2rem; font-weight: 600; color: var(--text); }
  .modal-close {
    background: none; border: none; font-size: 1.8rem; line-height: 1;
    color: var(--muted); cursor: pointer; padding: 4px 8px;
    transition: color 0.2s;
  }
  .modal-close:hover { color: var(--text); }
  .modal-body { padding: 20px; }
  .chart-wrap { margin: 16px 0; }
  .timeframes { display: flex; gap: 6px; margin-top: 12px; }
  .tf-btn {
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); border-radius: 6px; padding: 6px 12px;
    font-family: var(--font-data); font-size: 0.75rem; cursor: pointer;
    transition: all 0.2s;
  }
  .tf-btn:hover { background: var(--accent); color: #171710; }
  .tf-btn.active { background: var(--accent); color: #171710; }
  .stat-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
    margin-bottom: 12px;
  }
  .stat-item {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px; text-align: center;
  }
  .stat-label { font-size: 0.65rem; color: var(--muted); letter-spacing: .04em; }
  .stat-value { font-size: 0.85rem; font-weight: 600; margin-top: 2px; }
  .gain { color: var(--green); }
  .loss { color: var(--red); }
  .flat { color: var(--muted); }
  .vol-toggle {
    display: inline-flex; align-items: center; gap: 6px; font-size: .78rem;
    color: var(--muted); margin: 10px 0 4px; cursor: pointer; user-select: none;
  }
  .vol-toggle input { accent-color: var(--accent); cursor: pointer; }
  .ohlc-readout {
    display: flex; flex-wrap: wrap; gap: 14px; align-items: baseline;
    font: 600 .82rem/1 var(--font-data); color: var(--muted); margin-top: 10px;
  }
  .ohlc-readout b { color: var(--text); font-weight: 700; margin-right: 3px; }
  .ohlc-readout .pct { font-weight: 700; }
  .ohlc-stats-grid {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 14px;
  }
  @media (max-width: 560px) {
    .ohlc-stats-grid { grid-template-columns: repeat(2, 1fr); }
  }
</style>

<script>
let coinChart = null;
const COIN_INTERVALS = [
  {label: "1D", value: "1d"},
  {label: "7D", value: "7d"},
  {label: "30D", value: "30d"},
  {label: "90D", value: "90d"},
  {label: "1Y", value: "1y"},
];

function fmtPrice(v) {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v >= 1000) return "$" + v.toLocaleString(undefined, {maximumFractionDigits: 0});
  if (v >= 1) return "$" + v.toFixed(2);
  return "$" + v.toFixed(4);
}
function fmtPct(v) {
  if (v == null || !Number.isFinite(v)) return "—";
  return (v > 0 ? "+" : "") + v.toFixed(1) + "%";
}

async function loadCoinData(symbol) {
  try {
    const r = await fetch(`/api/coin/${encodeURIComponent(symbol)}`);
    if (!r.ok) throw new Error("coin not found");
    return await r.json();
  } catch(e) {
    console.warn("coin data error:", e);
    return null;
  }
}

async function loadCoinChart(symbol, interval) {
  try {
    const r = await fetch(`/api/chart/${encodeURIComponent(symbol)}?interval=${interval}`);
    if (!r.ok) throw new Error("chart not found");
    return await r.json();
  } catch(e) {
    console.warn("chart error:", e);
    return null;
  }
}

// Builds a compact SVG area-sparkline from a price series — deliberately not
// Chart.js here: up to 12 of these render at once on the list, and a full
// Chart.js instance per card (with its own canvas, animation loop, and
// resize observer) is needless overhead for something this small. A plain
// <polyline> + gradient fill gets the same "premium" look at a fraction of
// the cost, and there's zero risk of 12 concurrent chart instances
// stepping on each other.
function sparklineSVG(prices, gradientId) {
  const pts = (prices || []).map(p => p.price).filter(v => Number.isFinite(v));
  if (pts.length < 2) return { svg: '', isGain: null };

  const min = Math.min(...pts), max = Math.max(...pts);
  const span = (max - min) || 1;
  const w = 100, h = 32, pad = 2;
  const stepX = (w - pad * 2) / (pts.length - 1);
  const coords = pts.map((v, i) => {
    const x = pad + i * stepX;
    const y = pad + (1 - (v - min) / span) * (h - pad * 2);
    return [x, y];
  });
  const isGain = pts[pts.length - 1] >= pts[0];
  const lineColor = isGain ? 'var(--green)' : 'var(--red)';
  const linePath = coords.map((c, i) => (i === 0 ? 'M' : 'L') + c[0].toFixed(2) + ',' + c[1].toFixed(2)).join(' ');
  const fillPath = linePath + ` L${coords[coords.length-1][0].toFixed(2)},${h} L${coords[0][0].toFixed(2)},${h} Z`;

  const svg =
    `<defs><linearGradient id="${gradientId}" x1="0" y1="0" x2="0" y2="1">` +
      `<stop offset="0%" stop-color="${lineColor}" stop-opacity="0.32"/>` +
      `<stop offset="100%" stop-color="${lineColor}" stop-opacity="0"/>` +
    `</linearGradient></defs>` +
    `<path class="spark-fill" d="${fillPath}" fill="url(#${gradientId})"/>` +
    `<path class="spark-line" d="${linePath}" stroke="${lineColor}"/>`;
  return { svg, isGain };
}

async function hydrateCardChart(symbol) {
  const priceEl = document.getElementById('price-' + symbol);
  const sparkEl = document.getElementById('spark-' + symbol);
  if (!priceEl || !sparkEl) return;

  const [coin, chart] = await Promise.all([
    loadCoinData(symbol),
    loadCoinChart(symbol, '7d'),
  ]);

  if (coin && coin.price != null) {
    const chg = coin.change_24h;
    const chgClass = chg > 0 ? 'gain' : chg < 0 ? 'loss' : 'flat';
    priceEl.innerHTML =
      '<span class="px">' + fmtPrice(coin.price) + '</span>' +
      '<span class="chg ' + chgClass + '">' + fmtPct(chg) + '</span>';
  }

  if (chart && chart.prices && chart.prices.length >= 2) {
    const { svg } = sparklineSVG(chart.prices, 'spark-grad-' + symbol.replace(/[^A-Za-z0-9]/g, ''));
    sparkEl.innerHTML = svg;
  }
  sparkEl.classList.remove('is-loading');
}

function renderStats(data) {
  const stats = document.getElementById("modalStats");
  if (!data) {
    stats.innerHTML = '<div class="stat-item"><div class="stat-value">Data unavailable</div></div>';
    return;
  }
  stats.innerHTML = `
    <div class="stat-grid">
      <div class="stat-item"><div class="stat-label">Price</div><div class="stat-value">${fmtPrice(data.price)}</div></div>
      <div class="stat-item"><div class="stat-label">24h Change</div><div class="stat-value ${data.change_24h > 0 ? 'gain' : data.change_24h < 0 ? 'loss' : 'flat'}">${fmtPct(data.change_24h)}</div></div>
      <div class="stat-item"><div class="stat-label">7d Change</div><div class="stat-value ${data.change_7d > 0 ? 'gain' : data.change_7d < 0 ? 'loss' : 'flat'}">${fmtPct(data.change_7d)}</div></div>
      <div class="stat-item"><div class="stat-label">Market Cap</div><div class="stat-value">${data.market_cap ? fmtPrice(data.market_cap) : '—'}</div></div>
      <div class="stat-item"><div class="stat-label">24h Volume</div><div class="stat-value">${data.volume_24h ? fmtPrice(data.volume_24h) : '—'}</div></div>
      <div class="stat-item"><div class="stat-label">ATH</div><div class="stat-value">${data.ath ? fmtPrice(data.ath) : '—'}</div></div>
    </div>
  `;
}

async function loadCoinOHLC(symbol, interval) {
  try {
    const r = await fetch(`/api/ohlc/${encodeURIComponent(symbol)}?interval=${interval}`);
    if (!r.ok) throw new Error("ohlc not found");
    return await r.json();
  } catch(e) {
    console.warn("ohlc error:", e);
    return null;
  }
}

let lastCandles = null;
let lastCandleInterval = '1d';

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function drawCandlestickChart(candles, interval, showVolume) {
  const ctx = document.getElementById('coinChart').getContext('2d');
  if (coinChart) coinChart.destroy();

  const upColor = cssVar('--green', '#7fc49a');
  const downColor = cssVar('--red', '#dd8b83');
  const gridColor = 'rgba(139,148,158,0.1)';
  const tickColor = cssVar('--muted', '#98a49e');

  const ohlcData = candles.map(c => ({ x: c.t, o: c.o, h: c.h, l: c.l, c: c.c }));
  const hasVolume = showVolume && candles.some(c => c.v != null);

  const datasets = [{
    label: interval.toUpperCase(),
    data: ohlcData,
    color: { up: upColor, down: downColor, unchanged: tickColor },
    borderColor: { up: upColor, down: downColor, unchanged: tickColor },
    yAxisID: 'y',
  }];

  const scales = {
    x: { type: 'time', time: { tooltipFormat: 'PPpp' }, ticks: { color: tickColor, maxTicksLimit: 7 }, grid: { display: false } },
    y: { position: 'right', ticks: { color: tickColor, callback: v => '$' + v.toLocaleString(undefined, {maximumFractionDigits: 6}) }, grid: { color: gridColor } },
  };

  if (hasVolume) {
    datasets.push({
      type: 'bar',
      label: 'Volume',
      data: candles.map(c => ({ x: c.t, y: c.v || 0 })),
      backgroundColor: candles.map(c => c.c >= c.o ? upColor + '4d' : downColor + '4d'), // ~30% alpha
      yAxisID: 'volume',
      order: 2,
    });
    // Volume occupies only the bottom ~22% of the chart by giving its axis
    // a much taller max than the actual data needs.
    const maxVol = Math.max(...candles.map(c => c.v || 0), 1);
    scales.volume = { type: 'linear', position: 'left', display: false, min: 0, max: maxVol * 4.5 };
  }

  coinChart = new Chart(ctx, {
    type: 'candlestick',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
      },
      scales,
    }
  });
}

function renderOhlcReadout(candles) {
  const el = document.getElementById('ohlcReadout');
  if (!candles || candles.length === 0) { el.innerHTML = ''; return; }
  const last = candles[candles.length - 1];
  const first = candles[0];
  const pct = first.o ? ((last.c - first.o) / first.o) * 100 : 0;
  const pctClass = pct > 0 ? 'gain' : pct < 0 ? 'loss' : 'flat';
  el.innerHTML =
    `<span>O <b>${fmtPrice(last.o)}</b></span>` +
    `<span>H <b>${fmtPrice(last.h)}</b></span>` +
    `<span>L <b>${fmtPrice(last.l)}</b></span>` +
    `<span>C <b>${fmtPrice(last.c)}</b></span>` +
    `<span class="pct ${pctClass}">${fmtPct(pct)}</span>`;
}

function renderOhlcStats(periodCandles, dayCandles, interval) {
  const grid = document.getElementById('ohlcStatsGrid');
  if (!dayCandles || dayCandles.length === 0) { grid.innerHTML = ''; return; }

  const hi24 = Math.max(...dayCandles.map(c => c.h));
  const lo24 = Math.min(...dayCandles.map(c => c.l));
  const chg24 = dayCandles[0].o ? ((dayCandles[dayCandles.length-1].c - dayCandles[0].o) / dayCandles[0].o) * 100 : 0;

  let periodChg = chg24;
  if (periodCandles && periodCandles.length > 0 && periodCandles[0].o) {
    const pf = periodCandles[0], pl = periodCandles[periodCandles.length - 1];
    periodChg = ((pl.c - pf.o) / pf.o) * 100;
  }
  const periodLabel = (COIN_INTERVALS.find(iv => iv.value === interval) || {label: interval.toUpperCase()}).label;

  const tile = (label, value, cls) =>
    `<div class="stat-item"><div class="stat-label">${label}</div><div class="stat-value ${cls||''}">${value}</div></div>`;

  grid.innerHTML =
    tile('24H HIGH', fmtPrice(hi24)) +
    tile('24H LOW', fmtPrice(lo24)) +
    tile(periodLabel + ' CHANGE', fmtPct(periodChg), periodChg > 0 ? 'gain' : periodChg < 0 ? 'loss' : 'flat') +
    tile('24H CHANGE', fmtPct(chg24), chg24 > 0 ? 'gain' : chg24 < 0 ? 'loss' : 'flat');
}

function renderTimeframes(symbol, activeInterval) {
  const tf = document.getElementById("modalTimeframes");
  tf.innerHTML = COIN_INTERVALS.map(iv =>
    `<button class="tf-btn ${iv.value === activeInterval ? 'active' : ''}" onclick="showCoin('${symbol}', '${iv.value}')">${iv.label}</button>`
  ).join('');
}

async function showCoin(symbol, interval) {
  document.getElementById("coinModal").hidden = false;
  document.getElementById("modalCoinName").textContent = symbol;
  document.getElementById("modalClose").onclick = () => {
    document.getElementById("coinModal").hidden = true;
    if (coinChart) coinChart.destroy();
  };

  // Close on outside click
  document.getElementById("coinModal").onclick = (e) => {
    if (e.target === document.getElementById("coinModal")) {
      document.getElementById("coinModal").hidden = true;
      if (coinChart) coinChart.destroy();
    }
  };

  renderCoinIntel(symbol);
  const data = await loadCoinData(symbol);
  renderStats(data);

  // The 24H HIGH/LOW/CHANGE tiles always reflect the real last-24h window
  // regardless of which timeframe tab is selected, so on the 1D tab we
  // reuse that single fetch for both; any other tab needs its own fetch
  // plus a second one just for the always-on 24h tiles.
  const [ohlc, dayOhlc] = interval === '1d'
    ? await Promise.all([loadCoinOHLC(symbol, '1d'), Promise.resolve(null)])
    : await Promise.all([loadCoinOHLC(symbol, interval), loadCoinOHLC(symbol, '1d')]);

  if (ohlc && ohlc.candles && ohlc.candles.length > 0) {
    try {
      lastCandles = ohlc.candles;
      lastCandleInterval = interval;
      const showVolume = document.getElementById('volToggle').checked;
      drawCandlestickChart(ohlc.candles, interval, showVolume);
      renderOhlcReadout(ohlc.candles);
      renderOhlcStats(ohlc.candles, (dayOhlc && dayOhlc.candles) || ohlc.candles, interval);
      document.getElementById("chartNote").textContent = "Live OHLC candles \u00b7 Powered by " + (ohlc.source === 'binance' ? 'Binance' : 'CoinGecko');
    } catch (err) {
      // A rendering bug here must never take the rest of the modal down
      // with it (timeframe tabs, close button) - log it and show a plain
      // message instead of leaving the chart area silently blank.
      console.error('candlestick render error:', err);
      lastCandles = null;
      document.getElementById('ohlcReadout').innerHTML = '';
      document.getElementById('ohlcStatsGrid').innerHTML = '';
      document.getElementById("chartNote").textContent = "Chart render error \u2014 see console";
    }
  } else {
    lastCandles = null;
    if (coinChart) coinChart.destroy();
    document.getElementById('ohlcReadout').innerHTML = '';
    document.getElementById('ohlcStatsGrid').innerHTML = '';
    const ctx = document.getElementById('coinChart').getContext('2d');
    ctx.clearRect(0, 0, 800, 400);
    ctx.font = '14px monospace';
    ctx.fillStyle = '#98a49e';
    ctx.fillText('Chart unavailable \u2014 set COINGECKO_API_KEY in Render env', 20, 40);
    document.getElementById("chartNote").textContent = "";
  }
  renderTimeframes(symbol, interval);
}

document.getElementById('volToggle').addEventListener('change', (e) => {
  if (lastCandles) drawCandlestickChart(lastCandles, lastCandleInterval, e.target.checked);
});
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-chart-financial@0.2.1/dist/chartjs-chart-financial.min.js"></script>
</html>"""


@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "service": "super-crypto-agent"})


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML, agent_count=len(AGENTS_DATA))


AGENTS_DATA = [
    {"name": "Micro-Cap Finder", "emoji": "🔬", "key": "microcap"},
    {"name": "Whale Detector", "emoji": "⚡", "key": "whale"},
    {"name": "News Scanner", "emoji": "📰", "key": "news"},
    {"name": "Sentiment Agent", "emoji": "💭", "key": "sentiment"},
    {"name": "Pattern Recognition", "emoji": "📊", "key": "pattern"},
    {"name": "Correlation Monitor", "emoji": "🔗", "key": "correlation"},
    {"name": "Due Diligence", "emoji": "🛡️", "key": "dd"},
    {"name": "On-Chain Holders", "emoji": "⛓️", "key": "onchain"},
    {"name": "Macro Regime", "emoji": "🌐", "key": "macro"},
    {"name": "Meta-Learner", "emoji": "🧠", "key": "meta"},
    {"name": "Santiment Activity", "emoji": "🛰️", "key": "santiment"},
    {"name": "Derivatives Flow", "emoji": "📉", "key": "derivatives"},
    {"name": "Fresh Funding", "emoji": "💰", "key": "fundraising"},
    {"name": "Kronos Forecast", "emoji": "🔮", "key": "kronos"},
]


@app.route("/api/status")
def api_status():
    return jsonify({
        "running": scan_running,
        "last_run": scan_last_run,
        "last_error": scan_last_error,
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    return jsonify(run_scan_background())


@app.route("/api/report")
def api_report():
    md, path = get_latest_report()
    if md:
        ts = None
        fname = os.path.basename(path).replace("report_", "").replace(".md", "")
        try:
            # Report filenames are written in JST (see advisor._write_report)
            ts = datetime.strptime(fname, "%Y%m%d_%H%M%S").replace(tzinfo=JST).isoformat()
        except Exception:
            ts = datetime.fromtimestamp(os.path.getmtime(path), JST).isoformat(timespec="seconds")
        cards = parse_report_cards(md)
        # Enrich each card with which agents worked on the coin (and what they
        # saw) plus its Kronos forecast, so the dashboard can show them.
        verdicts = {v.get("coin"): v for v in (_read_json(VERDICTS_FILE, {}) or {}).get("verdicts", [])}
        kronos_latest = (_read_json(KRONOS_FILE, {}) or {}).get("latest") or {}
        for c in cards:
            v = verdicts.get(c["coin"]) or {}
            c["agents"] = v.get("agents") or []
            c["evidence"] = v.get("evidence") or []
            c["score"] = v.get("score")
            # The report is written before the risk manager runs; show the
            # final call (e.g. HOLD for coins already in the paper portfolio).
            if v.get("action"):
                c["action"] = v["action"]
            c["risk_notes"] = v.get("risk_notes") or []
            c["kronos"] = kronos_latest.get(c["coin"])
        return jsonify({"report": md, "cards": cards, "timestamp": ts})
    return jsonify({"report": None, "cards": [], "timestamp": None})


@app.route("/api/healthcheck")
def api_healthcheck():
    report = _read_json(os.path.join(DATA_DIR, "health.json"), {}) or {}
    return jsonify(report or {"overall": None, "checks": []})


@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(get_portfolio_summary())


@app.route("/api/kronos")
def api_kronos():
    return jsonify(get_kronos_summary())


@app.route("/api/signals")
def api_signals():
    return jsonify(get_signals_summary())


@app.route("/api/attribution")
def api_attribution():
    return jsonify({"agents": get_attribution_summary()})


@app.route("/api/agents")
def api_agents():
    return jsonify({"agents": AGENTS_DATA, "snapshots": get_agent_memory_snapshot()})


_TICKER_TO_CG = dict(KNOWN_IDS)
_TICKER_TO_CG.update({k.upper(): v for k, v in KNOWN_IDS.items()})

# Binance pair mapping for chart fallback (CoinGecko rate-limits Render IPs)
BN_BINANCE_PAIRS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "ADA": "ADAUSDT", "DOGE": "DOGEUSDT", "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT", "DOT": "DOTUSDT", "MATIC": "POLUSDT", "POL": "POLUSDT",
    "LTC": "LTCUSDT", "NEAR": "NEARUSDT", "APT": "APTUSDT", "ARB": "ARBUSDT",
    "OP": "OPUSDT", "SUI": "SUIUSDT", "UNI": "UNIUSDT", "AAVE": "AAVEUSDT",
    "MKR": "MKRUSDT", "PEPE": "PEPEUSDT", "SHIB": "SHIBUSDT", "BONK": "BONKUSDT",
    "TRX": "TRXUSDT", "FIL": "FILUSDT", "ATOM": "ATOMUSDT", "INJ": "INJUSDT",
    "ZEC": "ZECUSDT", "PENGU": "PENGUUSDT", "ETC": "ETCUSDT", "TAO": "TAOUSDT",
    "SUSHI": "SUSHIUSDT", "FTM": "FTMUSDT", "HBAR": "HBARUSDT", "SEI": "SEIUSDT",
    "IMX": "IMXUSDT", "THETA": "THETAUSDT", "CFX": "CFXUSDT", "KAS": "KASUSDT",
    "QNT": "QNTUSDT", "TIA": "TIAUSDT", "STRK": "STRKUSDT", "WLD": "WLDUSDT",
    "PYTH": "PYTHUSDT", "JUP": "JUPUSDT", "WIF": "WIFUSDT", "ICP": "ICPUSDT",
    "RNDR": "RNDRUSDT", "SNX": "SNXUSDT", "GRT": "GRTUSDT", "CRV": "CRVUSDT",
    "LIT": "LITUSDT", "STX": "STXUSDT", "KSM": "KSMUSDT",
}

_BN_KNOWN = set(BN_BINANCE_PAIRS.keys())
_INTERVAL_MAP = {"1d": 1800, "7d": 3600, "30d": 86400, "90d": 86400, "1y": 86400}
_INTERVAL_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "1y": 365}


def _binance_chart(symbol: str, interval: str) -> list:
    pair = BN_BINANCE_PAIRS.get(symbol.upper())
    if not pair:
        # Try symbolUSDT pattern for unknown coins
        pair = f"{symbol.upper()}USDT"
    if not pair:
        return []
    klines = _binance_klines(pair, _INTERVAL_MAP.get(interval, 1800))
    return [{"t": k[0], "price": float(k[4])} for k in klines]


def _binance_chart_ohlc(symbol: str, interval: str) -> list:
    """Same Binance klines as _binance_chart, but keeping the full OHLCV
    tuple instead of discarding everything but the close price. Binance's
    /klines endpoint already returns real candlestick data - there was no
    need for a second network call to get this."""
    pair = BN_BINANCE_PAIRS.get(symbol.upper())
    if not pair:
        pair = f"{symbol.upper()}USDT"
    if not pair:
        return []
    klines = _binance_klines(pair, _INTERVAL_MAP.get(interval, 1800))
    return [
        {"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
         "c": float(k[4]), "v": float(k[5])}
        for k in klines
    ]


def _coingecko_ohlc(symbol: str, interval: str) -> list:
    """Fallback OHLC source for coins not on Binance. CoinGecko's dedicated
    /coins/{id}/ohlc endpoint (distinct from /market_chart) returns real
    open/high/low/close - just no volume, which the frontend handles by
    simply not drawing a volume panel when v is null."""
    cid = _cg_id_for_coin(symbol)
    days = _INTERVAL_DAYS.get(interval, 1)
    params = {"vs_currency": "usd", "days": days}
    if CG_API_KEY:
        params["x_cg_demo_api_key"] = CG_API_KEY
    data = api_get(f"{COINGECKO_BASE}/coins/{cid}/ohlc", params=params, tries=2)
    if isinstance(data, list):
        return [
            {"t": row[0], "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": None}
            for row in data if isinstance(row, list) and len(row) >= 5
        ]
    return []


def _binance_klines(pair: str, interval_sec: int, limit: int = 500):
    interval_map = {60: "1m", 1800: "30m", 3600: "1h", 86400: "1d"}
    binterval = interval_map.get(interval_sec, "1d")
    data = api_get(f"{BINANCE_BASE}/klines", params={"symbol": pair, "interval": binterval, "limit": limit}, tries=2)
    if isinstance(data, list):
        return data
    return []


# KuCoin is a free, no-key Binance-style OHLCv source. Its /market/candles rows
# differ from Binance klines in two important ways: the field order is
#   [time, open, close, high, low, volume, turnover]   (note: close BEFORE high)
# and `time` is in SECONDS, not milliseconds. The two mappers below normalise
# both so the response envelopes below stay identical to Binance's.
_KUCOIN_TYPES = {1800: "30min", 3600: "1hour", 86400: "1day"}


def _kucoin_klines(pair: str, ktype: str, tries: int = 2) -> list:
    data = api_get(f"{KUCOIN_BASE}/market/candles",
                   params={"symbol": pair, "type": ktype}, tries=tries)
    if isinstance(data, dict) and data.get("code") == "200000":
        return data.get("data") or []
    return []


def _kucoin_chart_prices(symbol: str, interval: str) -> list:
    """Close-price series matching _binance_chart's shape, ms timestamps."""
    pair = f"{symbol.upper()}-USDT"
    ktype = _KUCOIN_TYPES.get(_INTERVAL_MAP.get(interval, 1800), "1day")
    out = []
    for r in _kucoin_klines(pair, ktype):
        if isinstance(r, list) and len(r) >= 6:
            c = float(r[2])
            if c > 0:
                out.append({"t": int(float(r[0]) * 1000), "price": round(c, 4)})
    return out


def _kucoin_ohlc(symbol: str, interval: str) -> list:
    """Full OHLCV candles matching _binance_chart_ohlc's shape, ms timestamps."""
    pair = f"{symbol.upper()}-USDT"
    ktype = _KUCOIN_TYPES.get(_INTERVAL_MAP.get(interval, 1800), "1day")
    return [
        {"t": int(float(r[0]) * 1000), "o": float(r[1]), "h": float(r[3]),
         "l": float(r[4]), "c": float(r[2]), "v": float(r[5])}
        for r in _kucoin_klines(pair, ktype)
        if isinstance(r, list) and len(r) >= 6
    ]


def _cg_id_for_coin(symbol: str) -> str:
    s = symbol.upper()
    if s in _TICKER_TO_CG:
        return _TICKER_TO_CG[s]
    cid = coin_id_for(s)
    return cid or s.lower()


@app.route("/api/coin/<symbol>")
def api_coin(symbol: str):
    symbol = symbol.upper()
    cache_key = f"coin:{symbol}"
    cached = _cached_get(cache_key, ttl=300)
    if cached is not None:
        return jsonify(cached)

    # Try market cache first (pre-warmed on startup)
    if symbol in _market_cache:
        c = _market_cache[symbol]
        result = {
            "symbol": c.get("symbol", "").upper(),
            "name": c.get("name", ""),
            "image": c.get("image", ""),
            "price": c.get("current_price"),
            "change_24h": c.get("price_change_percentage_24h"),
            "change_7d": c.get("price_change_percentage_7d"),
            "ath": c.get("ath"),
            "ath_change": c.get("ath_change_percentage"),
            "market_cap": c.get("market_cap"),
            "volume_24h": c.get("total_volume"),
            "circulating_supply": c.get("circulating_supply"),
            "total_supply": c.get("total_supply"),
            "source": "markets_cache",
        }
        _cached_set(cache_key, result)
        return jsonify(result)

    # Fallback: fetch individual coin, then Binance 24h ticker
    cid = _cg_id_for_coin(symbol)
    params = {"localization": "false", "tickers": "false",
              "community_data": "false", "developer_data": "false", "sparkline": "false"}
    if CG_API_KEY:
        params["x_cg_demo_api_key"] = CG_API_KEY
    data = api_get(f"{COINGECKO_BASE}/coins/{cid}", params=params, tries=2)
    if isinstance(data, dict) and "market_data" in data:
        md = data.get("market_data", {})
        ch24 = md.get("price_change_percentage_24h")
        ch7 = md.get("price_change_percentage_7d") or (md.get("price_change_percentage_7d_in_currency") or {}).get("usd")
        ch30 = md.get("price_change_percentage_30d") or (md.get("price_change_percentage_30d_in_currency") or {}).get("usd")
        result = {
            "symbol": data.get("symbol", "").upper(),
            "name": data.get("name", ""),
            "image": (data.get("image") or {}).get("large", ""),
            "price": md.get("current_price", {}).get("usd"),
            "change_24h": ch24 if isinstance(ch24, (int, float)) else (ch24 or {}).get("usd"),
            "change_7d": ch7,
            "change_30d": ch30,
            "ath": md.get("ath", {}).get("usd"),
            "ath_change": md.get("ath_change_percentage"),
            "market_cap": md.get("market_cap", {}).get("usd"),
            "volume_24h": md.get("total_volume", {}).get("usd"),
            "circulating_supply": md.get("circulating_supply"),
            "total_supply": md.get("total_supply"),
            "source": "coingecko",
        }
    else:
        # Binance fallback for 24h ticker
        pair = BN_BINANCE_PAIRS.get(symbol.upper())
        if pair:
            bn_data = api_get(f"{BINANCE_BASE}/ticker/24hr", params={"symbol": pair}, tries=1)
            if isinstance(bn_data, dict):
                result = {
                    "symbol": symbol.upper(),
                    "name": symbol.upper(),
                    "image": "",
                    "price": float(bn_data.get("lastPrice", 0)),
                    "change_24h": float(bn_data.get("priceChangePercent", 0)),
                    "change_7d": None,
                    "ath": None,
                    "ath_change": None,
                    "market_cap": float(bn_data.get("quoteVolume", 0)) * 100,
                    "volume_24h": float(bn_data.get("quoteVolume", 0)),
                    "circulating_supply": None,
                    "total_supply": None,
                    "source": "binance",
                }
            else:
             result = {"error": "coin not found"}
        else:
            # Try dynamic symbolUSDT pair
            pair = f"{symbol.upper()}USDT"
            bn_data = api_get(f"{BINANCE_BASE}/ticker/24hr", params={"symbol": pair}, tries=1)
            if isinstance(bn_data, dict) and "lastPrice" in bn_data:
                result = {
                    "symbol": symbol.upper(),
                    "name": symbol.upper(),
                    "image": "",
                    "price": float(bn_data.get("lastPrice", 0)),
                    "change_24h": float(bn_data.get("priceChangePercent", 0)),
                    "change_7d": None,
                    "ath": None,
                    "ath_change": None,
                    "market_cap": float(bn_data.get("quoteVolume", 0)) * 100,
                    "volume_24h": float(bn_data.get("quoteVolume", 0)),
                    "circulating_supply": None,
                    "total_supply": None,
                    "source": "binance",
                }
            else:
                result = {"error": "coin not found"}
    _cached_set(cache_key, result)
    return jsonify(result)


@app.route("/api/chart/<symbol>")
def api_chart(symbol: str):
    interval = __import__("flask").request.args.get("interval", "1d")
    days = _INTERVAL_DAYS.get(interval, 1)
    cache_key = f"chart:{symbol.upper()}:{days}"
    cached = _cached_get(cache_key, ttl=3600)
    if cached is not None:
        return jsonify(cached)

    # Try Binance first (most reliable on Render's shared IP)
    bn_prices = _binance_chart(symbol, interval)
    if bn_prices and len(bn_prices) >= 2:
        result = {
            "symbol": symbol.upper(), "interval": interval, "days": days,
            "prices": bn_prices, "source": "binance",
        }
        _cached_set(cache_key, result)
        return jsonify(result)

    # Next: KuCoin (free, no key) — covers non-Binance coins that CoinGecko's
    # free tier frequently throttles when the page requests many coins at once.
    kc_prices = _kucoin_chart_prices(symbol, interval)
    if kc_prices and len(kc_prices) >= 2:
        result = {
            "symbol": symbol.upper(), "interval": interval, "days": days,
            "prices": kc_prices, "source": "kucoin",
        }
        _cached_set(cache_key, result)
        return jsonify(result)

    # Last resort: CoinGecko (with retries via api_get for rate-limit handling)
    cid = _cg_id_for_coin(symbol)
    params = {"vs_currency": "usd", "days": days}
    if days >= 2:
        params["interval"] = "daily"
    if CG_API_KEY:
        params["x_cg_demo_api_key"] = CG_API_KEY
    data = api_get(f"{COINGECKO_BASE}/coins/{cid}/market_chart", params=params, tries=3)
    if isinstance(data, dict) and "prices" in data:
        prices = data.get("prices", [])
        result = {
            "symbol": symbol.upper(), "interval": interval, "days": days,
            "prices": [{"t": p[0], "price": round(p[1], 4)} for p in prices if p[1] > 0],
            "source": "coingecko",
        }
        _cached_set(cache_key, result)
        return jsonify(result)

    return jsonify({"error": "chart data unavailable", "symbol": symbol.upper()}), 404


@app.route("/api/ohlc/<symbol>")
def api_ohlc(symbol: str):
    """Real candlestick (open/high/low/close/volume) data, distinct from
    /api/chart which only ever returns a close-price line series."""
    interval = __import__("flask").request.args.get("interval", "1d")
    if interval not in _INTERVAL_MAP:
        interval = "1d"
    cache_key = f"ohlc:{symbol.upper()}:{interval}"
    ttl = 180 if interval == "1d" else 900
    cached = _cached_get(cache_key, ttl=ttl)
    if cached is not None:
        return jsonify(cached)

    # Binance → KuCoin → CoinGecko. KuCoin sits between the two because it is
    # free/no-key and reliably serves the non-Binance meme/alt coins that
    # CoinGecko's free tier throttles during a multi-coin page burst.
    candles = _binance_chart_ohlc(symbol, interval)
    source = "binance"
    if not candles or len(candles) < 2:
        candles = _kucoin_ohlc(symbol, interval)
        source = "kucoin"
    if not candles or len(candles) < 2:
        candles = _coingecko_ohlc(symbol, interval)
        source = "coingecko"

    if not candles:
        return jsonify({"error": "OHLC data unavailable", "symbol": symbol.upper()}), 404

    result = {"symbol": symbol.upper(), "interval": interval, "candles": candles, "source": source}
    _cached_set(cache_key, result)
    return jsonify(result)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Super Crypto Agent — Web Dashboard")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(ATTRIB_DIR, exist_ok=True)
    app.run(host=args.host, port=args.port, threaded=True)
