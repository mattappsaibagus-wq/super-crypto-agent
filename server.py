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
CG_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()

# Simple in-memory cache for chart/coin data (60s TTL)
_chart_cache = {}


def _cached_get(key: str, ttl: int = 60):
    entry = _chart_cache.get(key)
    if entry and time.time() - entry["t"] < ttl:
        return entry["data"]
    return None


def _cached_set(key: str, data):
    _chart_cache[key] = {"data": data, "t": time.time()}
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")
ATTRIB_DIR = os.path.join(DATA_DIR, "attribution")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")

app = Flask(__name__)

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
            scan_running = False

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
      document.getElementById('cards').innerHTML = '<div class="empty">No report yet</div>';
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
    cards.slice(0, 12).forEach(c => {
      const badgeClass = c.action === 'BUY' ? 'badge-buy' :
                         c.action === 'WATCH' ? 'badge-watch' :
                         c.action === 'SELL' ? 'badge-sell' : 'badge-avoid';
      const details = (c.details||[]).map(d => '<li>' + d + '</li>').join('');
      html += '<div class="card">' +
        '<div class="card-header">' +
          '<span class="coin-name">' + c.coin + '</span>' +
          '<span class="action-badge ' + badgeClass + '">' + c.action + '</span>' +
        '</div>' +
        '<ul class="card-details">' + details + '</ul>' +
      '</div>';
    });
    document.getElementById('cards').innerHTML = html || '<div class="empty">No results</div>';
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

loadReportWithClick();
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
      <div class="chart-wrap">
        <canvas id="coinChart" width="800" height="400"></canvas>
      </div>
      <div class="timeframes" id="modalTimeframes"></div>
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
</style>

<script>
let coinChart = null;
const COIN_INTERVALS = [
  {label: "1D", value: "1d"},
  {label: "7D", value: "7d"},
  {label: "30D", value: "30d"},
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

function drawChart(prices, interval) {
  const ctx = document.getElementById('coinChart').getContext('2d');
  if (coinChart) coinChart.destroy();
  const ds = prices.map(p => ({x: new Date(p.t), y: p.price}));
  coinChart = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [{
        label: interval.toUpperCase(),
        data: ds,
        borderColor: ds[ds.length-1]?.y >= ds[0]?.y ? '#7fc49a' : '#dd8b83',
        backgroundColor: 'rgba(127,196,154,0.08)',
        borderWidth: 2,
        pointRadius: 0,
        fill: true,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      parsing: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => `$${ctx.raw.y.toFixed(2)}`
          }
        }
      },
      scales: {
        x: { type: 'time', time: {tooltipFormat: 'PPpp'}, ticks: {color: '#98a49e', maxTicks: 6}, grid: {display: false}},
        y: { position: 'right', ticks: {color: '#98a49e', callback: v => '$' + v.toLocaleString()}, grid: {color: 'rgba(139,148,158,0.1)'}},
      }
    }
  });
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

  const chart = await loadCoinChart(symbol, interval);
  if (chart && chart.prices) {
    drawChart(chart.prices, interval);
  } else {
    const ctx = document.getElementById('coinChart').getContext('2d');
    if (coinChart) coinChart.destroy();
    ctx.clearRect(0, 0, 800, 400);
    ctx.font = '14px monospace';
    ctx.fillStyle = '#98a49e';
    ctx.fillText('Chart data unavailable — CoinGecko API limits', 20, 40);
  }
  renderTimeframes(symbol, interval);
}

// Make cards clickable
async function loadReportWithClick() {
  try {
    const r = await fetch('/api/report');
    const data = await r.json();
    if (!data.report) {
      document.getElementById('cards').innerHTML = '<div class="empty">No report yet</div>';
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
    cards.slice(0, 12).forEach(c => {
      const badgeClass = c.action === 'BUY' ? 'badge-buy' :
                         c.action === 'WATCH' ? 'badge-watch' :
                         c.action === 'SELL' ? 'badge-sell' : 'badge-avoid';
      const details = (c.details||[]).map(d => '<li>' + d + '</li>').join('');
      html += '<div class="card" onclick="showCoin(\'' + c.coin + '\', \'1d\')">' +
        '<div class="card-header">' +
          '<span class="coin-name">' + c.coin + '</span>' +
          '<span class="action-badge ' + badgeClass + '">' + c.action + '</span>' +
        '</div>' +
        '<ul class="card-details">' + details + '</ul>' +
      '</div>';
    });
    document.getElementById('cards').innerHTML = html || '<div class="empty">No results</div>';
  } catch(e) {
    document.getElementById('cards').innerHTML = '<div class="empty">Failed to load report</div>';
  }
}
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
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


TICKER_TO_CG = dict(KNOWN_IDS)
TICKER_TO_CG.update({
    k.upper(): v for k, v in KNOWN_IDS.items()
})

_INTERVAL_DAYS = {"1d": 1, "7d": 7, "30d": 30, "1y": 365}


def _cg_id_for_coin(symbol: str) -> str:
    s = symbol.upper()
    if s in TICKER_TO_CG:
        return TICKER_TO_CG[s]
    cid = coin_id_for(s)
    return cid or s.lower()


@app.route("/api/coin/<symbol>")
def api_coin(symbol: str):
    cache_key = f"coin:{symbol.upper()}"
    cached = _cached_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    cid = _cg_id_for_coin(symbol)
    params = {
        "localization": "false", "tickers": "false",
        "community_data": "false", "developer_data": "false", "sparkline": "false",
    }
    if CG_API_KEY:
        params["x_cg_demo_api_key"] = CG_API_KEY
    data = api_get(f"{COINGECKO_BASE}/coins/{cid}", params=params, tries=2)
    if not isinstance(data, dict) or "market_data" not in data:
        return jsonify({"error": "coin not found"}), 404
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
    }
    _cached_set(cache_key, result)
    return jsonify(result)


@app.route("/api/chart/<symbol>")
def api_chart(symbol: str):
    interval = __import__("flask").request.args.get("interval", "1d")
    days = _INTERVAL_DAYS.get(interval, 1)
    cache_key = f"chart:{symbol.upper()}:{days}"
    cached = _cached_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    cid = _cg_id_for_coin(symbol)
    params = {"vs_currency": "usd", "days": days}
    if days >= 2:
        params["interval"] = "daily"
    if CG_API_KEY:
        params["x_cg_demo_api_key"] = CG_API_KEY
    data = api_get(
        f"{COINGECKO_BASE}/coins/{cid}/market_chart",
        params=params,
        tries=2,
    )
    if not isinstance(data, dict) or "prices" not in data:
        return jsonify({"error": "chart data not found"}), 404
    prices = data.get("prices", [])
    result = {
        "symbol": symbol.upper(),
        "interval": interval,
        "days": days,
        "prices": [{"t": p[0], "price": round(p[1], 4)} for p in prices],
    }
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
