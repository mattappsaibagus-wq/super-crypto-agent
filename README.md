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
