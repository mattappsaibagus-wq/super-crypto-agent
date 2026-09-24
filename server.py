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
from supercrypto.config import KNOWN_IDS
from supercrypto.core.base import api_get, coin_id_for

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_BASE = "https://data-api.binance.vision/api/v3"
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
    global _market_cache
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
    global scan_running, scan_last_run, scan_last_error
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
            scan_last_run = datetime.now().isoformat()
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
    for name in ["whale", "sentiment", "pattern", "correlation", "news", "dd", "macro", "meta", "advisor"]:
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
  .card-details li { margin-bottom: 4px; list-style: none; }
  .card-details li::before { content: "· "; color: var(--accent); }
  .empty { text-align: center; padding: 40px; color: var(--muted); }
  .updated { margin-top: 24px; text-align: center; color: var(--muted); font-size: .75rem; }
  h2.section-title { font-size: 1.1rem; margin: 28px 0 14px; }
  .attribution-table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
  .attribution-table th, .attribution-table td {
    padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border);
    font-family: var(--font-data); font-size: .8rem;
  }
  .attribution-table th { color: var(--muted); font-weight: 500; }
  .attribution-table tr:hover { background: rgba(213,184,120,.03); }

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
</header>

<div class="status-bar">
  <span>📡 Signal bus active</span>
  <span class="badge-count" id="signalCount">0 signals</span>
</div>

<button class="btn" id="runBtn" onclick="startScan()">▶ Run Full Scan</button>

<div id="scanStatus" style="text-align:center;color:var(--muted);font-size:0.82rem;margin-bottom:16px">Idle</div>

<h2 class="section-title">🤖 Agent Team ({{ agent_count }} agents)</h2>
<div class="agents-grid" id="agentGrid"></div>

<h2 class="section-title">📊 Verdict Summary</h2>
<div class="summary-row" id="summaryRow" style="display:none">
  <div class="summary-chip chip-buy" id="buyCount">0<br><small>BUY</small></div>
  <div class="summary-chip chip-watch" id="watchCount">0<br><small>WATCH</small></div>
  <div class="summary-chip chip-sell" id="sellCount">0<br><small>SELL</small></div>
  <div class="summary-chip chip-avoid" id="avoidCount">0<br><small>AVOID</small></div>
</div>

<h2 class="section-title">🪙 Verdicts</h2>
<div id="cards"><div class="empty">No report yet — run a scan to get started</div></div>

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
];

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

async function loadReport() {
    try {
        const r = await fetch('/api/report');
        const data = await r.json();
        if (!data.report) {
            document.getElementById('cards').innerHTML = '<div class="empty">No report yet â run a scan to get started</div>';
            return;
        }
        document.getElementById('updated').textContent =
            'Last scan: ' + new Date(data.timestamp).toLocaleString();

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
        const symbols = [];
        cards.slice(0, 12).forEach(c => {
            const badgeClass = c.action === 'BUY' ? 'badge-buy' :
                               c.action === 'WATCH' ? 'badge-watch' :
                               c.action === 'SELL' ? 'badge-sell' : 'badge-avoid';
            const details = (c.details||[]).map(d => '<li>' + d + '</li>').join('');
            const sym = c.coin;
            symbols.push(sym);
            html += '<div class="card" onclick="showCoin(\'' + sym + '\', \'1d\')">' +
                '<div class="card-header">' +
                  '<span class="coin-name">' + sym + '</span>' +
                  '<span class="action-badge ' + badgeClass + '">' + c.action + '</span>' +
                '</div>' +
                '<div class="card-price-row" id="price-' + sym + '"></div>' +
                '<svg class="card-spark is-loading" id="spark-' + sym + '" viewBox="0 0 100 32" preserveAspectRatio="none"></svg>' +
                '<ul class="card-details">' + details + '</ul>' +
              '</div>';
        });
        document.getElementById('cards').innerHTML = html || '<div class="empty">No results</div>';

        // Price + sparkline hydrate progressively after the list itself is
        // already visible, so a slow/cold chart fetch never delays the
        // initial render. These mostly hit the server's warm cache (see
        // prewarm_chart_cache/prewarm_microcap_cache) so they're normally
        // near-instant, but staying async keeps things resilient either way.
        symbols.forEach(sym => hydrateCardChart(sym));
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

loadReport();
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
    scales.volume = { position: 'left', display: false, min: 0, max: maxVol * 4.5 };
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
    lastCandles = ohlc.candles;
    lastCandleInterval = interval;
    const showVolume = document.getElementById('volToggle').checked;
    drawCandlestickChart(ohlc.candles, interval, showVolume);
    renderOhlcReadout(ohlc.candles);
    renderOhlcStats(ohlc.candles, (dayOhlc && dayOhlc.candles) || ohlc.candles, interval);
    document.getElementById("chartNote").textContent = "Live OHLC candles · Powered by " + (ohlc.source === 'binance' ? 'Binance' : 'CoinGecko');
  } else {
    lastCandles = null;
    if (coinChart) coinChart.destroy();
    document.getElementById('ohlcReadout').innerHTML = '';
    document.getElementById('ohlcStatsGrid').innerHTML = '';
    const ctx = document.getElementById('coinChart').getContext('2d');
    ctx.clearRect(0, 0, 800, 400);
    ctx.font = '14px monospace';
    ctx.fillStyle = '#98a49e';
    ctx.fillText('Chart unavailable — set COINGECKO_API_KEY in Render env', 20, 40);
    document.getElementById("chartNote").textContent = "";
  }
  renderTimeframes(symbol, interval);
}

document.getElementById('volToggle').addEventListener('change', (e) => {
  if (lastCandles) drawCandlestickChart(lastCandles, lastCandleInterval, e.target.checked);
});
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.2.0/dist/index.min.js"></script>
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
            ts = datetime.strptime(fname, "%Y%m%d_%H%M%S").isoformat()
        except Exception:
            ts = datetime.now().isoformat()
        return jsonify({"report": md, "cards": parse_report_cards(md), "timestamp": ts})
    return jsonify({"report": None, "cards": [], "timestamp": None})


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

    # Fallback: CoinGecko (with retries via api_get for rate-limit handling)
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

    candles = _binance_chart_ohlc(symbol, interval)
    source = "binance"
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
