# Super Crypto Agent

⚡ An enhanced multi-agent system for cryptocurrency research and analysis. Ten specialized agents scan the market, perform due diligence, and consolidate signals into clear **BUY / WATCH / AVOID** verdicts — all with self-improving learning.

## The Agents

| Agent | Emoji | Job |
|-------|-------|-----|
| **Micro-Cap Finder** | 🔬 | Finds micro-caps with real volume and early momentum |
| **Whale Detector** | ⚡ | Spots big 24h moves, volume spikes, on-chain whale transfers |
| **News Scanner** | 📰 | Multi-source news (CoinPaprika + NewsAPI) with credibility scoring |
| **Sentiment Agent** | 💭 | Aggregates Reddit + Twitter + news sentiment per coin |
| **Pattern Recognition** | 📊 | Detects double-bottoms, head-and-shoulders, volume breakouts |
| **Correlation Monitor** | 🔗 | Watches BTC/alt correlation spikes and dumps |
| **Due Diligence** | 🛡️ | Verifies liquidity, holder concentration, market traction |
| **On-Chain Holders** | ⛓️ | Checks token holder concentration via Etherscan |
| **Macro Regime** | 🌐 | Fear & Greed + BTC dominance → risk_on/neutral/risk_off |
| **Meta-Learner** | 🧠 | Tracks agent accuracy, adjusts weights, detects regime shifts |
| **Santiment Activity** | 🛰️ | Dev-activity and active-address spikes (plus social volume with a key) |
| **Derivatives Flow** | 📉 | Hyperliquid funding + open-interest: squeezes, build-ups, crowded longs, flushes |
| **Fresh Funding** | 💰 | New VC rounds for tradable tokens from crypto-fundraising.info |
| **Kronos Forecast** | 🔮 | Foundation-model 24h forecasts from 4h candles ([Kronos](https://github.com/shiyu-coder/Kronos)) |

## Key Features

- **Regime-aware learning**: Separate weight tracks per market regime (risk_on / neutral / risk_off) so volatile periods don't corrupt bullish signals
- **Cross-agent knowledge sharing**: High-performing agents can boost signal weights for the whole team
- **Multi-dimensional rewards**: Weights adjust based on return, Sharpe ratio, and drawdown-adjusted performance
- **Attribution engine**: Decomposes P&L back to signal sources, flags overfit signals, and tracks agent performance
- **Calibration tracking**: Expected Calibration Error (ECE) measures confidence reliability

## Quick Start

```bash
pip install -r requirements.txt
python3 run_pipeline.py        # full scan from CLI
python3 server.py              # local dashboard on http://localhost:8080
```

### Options

```bash
python3 run_pipeline.py --coin BTC     # focus news on a specific coin
python3 run_pipeline.py --loop 6       # auto-scan every 6 hours
python3 server.py --interval 3         # dashboard auto-scan every 3h
```

## Web Dashboard

The Flask dashboard auto-deploys on [Render](https://render.com/):

- **Live scan control**: Trigger pipeline runs from the browser
- **Agent roster**: All 10 agents with status
- **Verdict cards**: BUY/WATCH/AVOID with per-coin reasoning
- **Attribution table**: P&L attribution by agent

### Deploy to Render

```bash
# 1. Connect repo to Render (or use render.yaml)
# 2. Build Command:  pip install -r requirements.txt
# 3. Start Command:  gunicorn server:app
```

Or with the Render CLI:

```bash
render deploy --service-name super-crypto-agent
```

## Data Sources (free, no API keys required)

- **CoinGecko** API — prices, market caps, OHLCV
- **CoinPaprika** — news search
- **DEXScreener** — pair liquidity data
- **Alternative.me** — Fear & Greed Index
- **Reddit / Twitter** — sentiment (optional)
- **NewsAPI** — news headlines (requires `NEWSAPI_KEY`)
- **Etherscan** — on-chain whale transfers (requires `ETHERSCAN_API_KEY`)
- **Santiment** — `dev_activity_1d` + `daily_active_addresses` (free, real-time, no key); set `SANTIMENT_API_KEY` to also try `social_volume_total`. Capped at 900 calls/month.
- **Hyperliquid** — funding, open interest, mark price for every perp in one free call (the same data Buildix charts)
- **crypto-fundraising.info** — public deal-flow table (newest ~10 rounds); scraped, low weight

## Kronos Forecast agent 🔮

[Kronos](https://github.com/shiyu-coder/Kronos) (MIT) is a decoder-only Transformer
pre-trained on K-line (OHLCV) data from 45+ exchanges, crypto included. Its inference
code is vendored under `vendor/kronos/`; the `Kronos-small` weights (~100 MB) download
from Hugging Face on first run and are cached by the scan workflow.

Each scan it forecasts the majors (BTC, ETH, SOL, BNB, XRP) plus every coin the other
agents surfaced (up to 30), using the last 60 days of **4h candles** (Binance public
data API, KuCoin fallback):

- 8 independent sample paths per coin, 24h ahead (6 bars) — the same horizon the
  learning loop grades.
- Emits `kronos_forecast_up` / `kronos_forecast_down` only when ≥75% of paths agree,
  the mean move clears 1% and a t-stat of 2.5 across paths, and the move isn't
  implausible (>4× the coin's daily volatility). Everything else is still shown on the
  dashboard as "no call".
- Every forecast is stored in `data/kronos_forecasts.json` and **graded 24h later**
  against the real close: direction hit rate, share inside the 10–90% band, average
  error, and the return from following the calls — overall, for signals only, and per
  coin. The WeightLearner also tracks both signals, so if Kronos has no edge its weight
  decays on its own.
- Without torch or the weights it sits out and the scan carries on. Disable it with
  `KRONOS_ENABLED=0`.

Local install (CPU-only torch):

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-kronos.txt
```

Dashboard: a **🔮 Kronos Forecasts** panel (track record + every coin's 24h path), a
Kronos line on each verdict card, and in each coin's detail view the forecast fan chart
plus **every agent working on that coin** with the signals and reasons it contributed.

## System health 🩺

After every scan `supercrypto/core/health.py` checks the symptoms of things
breaking and writes `data/health.json` (dashboard **System Health** panel):

| Check | Fails when |
|---|---|
| Signal freshness | expired signals are still on the bus (pruning broke) |
| BUY verdicts executed | a sized BUY didn't become a paper position (usually: no price) |
| Prices found | < 80% of needed prices found; also lists same-ticker tokens rejected / voided |
| Kronos forecasts | Kronos sat out or forecast 0 coins; warns if its hit rate < 45% after 30 graded |
| Verdicts produced | 0 verdicts (warns on a > 70% drop) |
| Agents producing signals | warns if microcap / dd / macro went silent |
| Paper drawdown | warns at 15% below peak |

`scripts/health_alert.py` (run by the scan workflow) opens a GitHub Issue labelled
**health-alert** when a check fails, updates it if the failures change, and closes
it when everything passes. `.github/workflows/health-watch.yml` runs hourly and
opens one if no scan has completed for 7 hours. GitHub emails you about new issues.

## Architecture

```
supercrypto/
├── config.py          # All tunables and signal registry
├── agents/            # The 10 specialized agents
│   └── __init__.py    # Exports new agents
├── core/
│   ├── base.py        # BaseAgent: signal bus + per-agent memory
│   ├── learning.py    # WeightLearner: regime-aware EMA + cross-agent sharing
│   ├── scoring.py     # Pure scoring functions (live + backtest parity)
│   ├── attribution.py # Agent performance attribution engine
│   ├── risk.py        # Risk manager: DD-scaled sizing, circuit breakers
│   ├── paper.py        # Paper trader: positions, exits, equity curve
│   └── backtest.py     # Backtest engine on historical candles
```

## Tests

```bash
python3 tests/test_alphaforge.py    # 25 original tests
python3 tests/test_supercrypto.py   #  9 enhanced feature tests
python3 tests/test_kronos.py        # Kronos agent (offline, fake predictor)
```

## Self-Improvement Loop

1. Agents emit signals with confidence scores onto `data/signals.json`
2. After 24h / 7d horizons, outcomes settle via CoinGecko price lookup
3. `WeightLearner` updates signal weights using multi-dimensional rewards (return + Sharpe + drawdown)
4. `MetaLearner` tracks per-agent win rates and adjusts weights
5. `AttributionEngine` records trades, attributing P&L to contributing signals
6. Overfit detection flags signals that are heavily weighted but underperforming

---

*Personal research tool only — not financial advice. Crypto markets are volatile; past performance is not indicative of future results.*
