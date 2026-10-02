# Super Crypto Agent - Workflow Documentation

**Repository**: https://github.com/mattappsaibagus-wq/super-crypto-agent  
**Version**: Latest (as of 2026-10-02)  
**Type**: Multi-Agent Cryptocurrency Analysis System  
**Stack**: Python 3.11, Flask, Gunicorn

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Installation & Setup](#installation--setup)
4. [Pipeline Workflow](#pipeline-workflow)
5. [Agent Responsibilities](#agent-responsibilities)
6. [API Endpoints](#api-endpoints)
7. [Deployment Workflow](#deployment-workflow)
8. [Development Workflow](#development-workflow)
9. [Testing Strategy](#testing-strategy)
10. [Data Flow](#data-flow)
11. [Configuration](#configuration)

---

## 🎯 Overview

Super Crypto Agent is an enhanced multi-agent system that performs cryptocurrency research and analysis. Ten specialized agents scan the market, perform due diligence, and consolidate signals into clear **BUY / WATCH / AVOID** verdicts with self-improving learning capabilities.

### Key Features

- **10 Specialized Agents** working in parallel
- **Regime-Aware Learning** - separate weight tracks per market regime
- **Cross-Agent Knowledge Sharing** - high-performing agents boost team signals
- **Multi-Dimensional Rewards** - adjusts based on return, Sharpe ratio, and drawdown
- **Attribution Engine** - decomposes P&L back to signal sources
- **Web Dashboard** - real-time monitoring and control interface

---

## 🏗 System Architecture

```
super-crypto-agent/
├── server.py                    # Flask web dashboard (main entry point)
├── run_pipeline.py              # CLI orchestrator for agent pipeline
├── run_backtest.py              # Single backtest runner
├── run_backtest_portfolio.py   # Portfolio-level backtest
├── requirements.txt             # Python dependencies
├── render.yaml                  # Render.com deployment config
│
├── supercrypto/
│   ├── config.py               # All tunables and signal registry
│   │
│   ├── agents/                 # 10 Specialized Agents
│   │   ├── __init__.py
│   │   ├── microcap.py         # 🔬 Finds micro-caps with volume/momentum
│   │   ├── whale.py            # ⚡ Detects whale moves & volume spikes
│   │   ├── news.py             # 📰 Multi-source news with credibility
│   │   ├── sentiment.py        # 💭 Reddit + Twitter + news sentiment
│   │   ├── pattern.py          # 📊 Technical pattern recognition
│   │   ├── correlation.py      # 🔗 BTC/alt correlation monitoring
│   │   ├── dd.py               # 🛡️ Due diligence verification
│   │   ├── onchain.py          # ⛓️ On-chain holder concentration
│   │   ├── macro.py            # 🌐 Macro regime detection
│   │   ├── meta_learner.py     # 🧠 Agent performance tracking
│   │   └── advisor.py          # Investment verdict generator
│   │
│   └── core/                   # Core Systems
│       ├── base.py             # BaseAgent: signal bus + memory
│       ├── learning.py         # WeightLearner: regime-aware EMA
│       ├── scoring.py          # Pure scoring functions
│       ├── attribution.py      # Performance attribution engine
│       ├── risk.py             # Risk manager: sizing & breakers
│       ├── paper.py            # Paper trader: positions & equity
│       └── backtest.py         # Backtest engine on historical data
│
├── data/                       # Runtime Data (gitignored)
│   ├── signals.json            # Signal bus (cleared each run)
│   ├── watchlist.json          # Tracked coins
│   ├── token_contracts.json    # Contract addresses
│   ├── paper_portfolio.json    # Paper trading state
│   ├── market_cache.json       # Prewarmed market data
│   ├── memory/                 # Per-agent weight memory
│   ├── reports/                # Generated markdown reports
│   └── attribution/            # Trade attribution history
│
└── tests/
    ├── test_supercrypto.py     # Enhanced feature tests (9 tests)
    └── test_alphaforge.py      # Original tests (25 tests)
```

---

## 🚀 Installation & Setup

### Prerequisites

- Python 3.11+
- pip package manager
- (Optional) API keys for enhanced features

### Local Installation

```bash
# 1. Clone the repository
git clone https://github.com/mattappsaibagus-wq/super-crypto-agent.git
cd super-crypto-agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create data directories (auto-created on first run)
mkdir -p data/{memory,reports,attribution}

# 4. (Optional) Set API keys for enhanced features
export COINGECKO_API_KEY="your_key_here"
export ETHERSCAN_API_KEY="your_key_here"
export NEWSAPI_KEY="your_key_here"
```

### Dependencies

```txt
flask
gunicorn
requests
```

**Free APIs (no keys required)**:
- CoinGecko (prices, market caps, OHLCV)
- CoinPaprika (news search)
- DEXScreener (pair liquidity)
- Alternative.me (Fear & Greed Index)
- Binance (price charts, OHLC data)

**Optional APIs (keys required for full features)**:
- NewsAPI (news headlines) - `NEWSAPI_KEY`
- Etherscan (on-chain whale transfers) - `ETHERSCAN_API_KEY`
- CoinGecko Pro (higher rate limits) - `COINGECKO_API_KEY`

---

## 🔄 Pipeline Workflow

### Pipeline Execution Order

```
[0] Macro Regime Detection
     ↓
[1-a] Sentiment Analysis (optional, slow)
[1-b] Pattern Recognition (optional, slow)
[1-c] Correlation Monitoring (optional, slow)
[1-d] Discovery Phase
      ├── Micro-Cap Finder
      ├── Whale Detector
      └── News Scanner
     ↓
[2] Due Diligence Verification
[2b] On-Chain Holder Analysis (requires ETHERSCAN_API_KEY)
[2c] Whale On-Chain Transfers (requires ETHERSCAN_API_KEY)
     ↓
[3] Meta-Learner (agent performance adjustment)
     ↓
[4] Investment Advisor (verdict generation)
     ↓
[5] Risk Management (position sizing, circuit breakers)
     ↓
[6] Paper Trading (simulate positions, track equity)
     ↓
[Output] Reports + Attribution
```

### Running the Pipeline

#### CLI Mode

```bash
# Single scan
python3 run_pipeline.py

# Focus on specific coin for news
python3 run_pipeline.py --coin BTC

# Auto-scan every 6 hours
python3 run_pipeline.py --loop 6

# Quick mode (skip slow agents: sentiment, pattern, correlation)
python3 run_pipeline.py --quick

# Keep prior signals (don't clear bus)
python3 run_pipeline.py --no-clear
```

#### Web Dashboard Mode

```bash
# Start local dashboard
python3 server.py                # http://localhost:8080

# Custom port
python3 server.py --port 9000

# Dashboard features:
# - Trigger scans from browser
# - View all 10 agents with status
# - See BUY/WATCH/AVOID verdicts
# - Monitor P&L attribution by agent
# - Interactive coin charts (OHLC candlesticks)
```

---

## 🤖 Agent Responsibilities

### 1. 🔬 Micro-Cap Finder (`microcap.py`)

**Purpose**: Discovers micro-caps with real volume and early momentum

**Signals Emitted**:
- `microcap_opportunity` - Found a viable micro-cap
- `strong_volume_ratio` - Volume > threshold
- `low_liquidity_risk` - Adequate liquidity
- `age_signal` - Survived long enough
- `trending_boost` - Appears in trending
- `dex_listing` - Listed on DEX

**Data Sources**: CoinGecko markets API, DEXScreener

**Configurable Parameters**:
- `DEFAULT_TOP_N_COINS = 200`
- `DEFAULT_VOL_THRESHOLD = 50,000`
- `DEFAULT_MCAP_MAX = 50,000,000`

---

### 2. ⚡ Whale Detector (`whale.py`)

**Purpose**: Spots big 24h moves, volume spikes, and on-chain whale transfers

**Signals Emitted**:
- `whale_up` / `whale_down` - Price moves
- `whale_onchain` / `whale_onchain_down` - Large transfers
- `volume_spike` - Unusual volume
- `price_move` - Significant price change
- `momentum` - Momentum score
- `liquidity_depth` - Liquidity check
- `sustainability` - Move sustainability

**Data Sources**: CoinGecko, Binance ticker, Etherscan (on-chain)

**Thresholds**:
- Price move: >5%
- Volume spike: >2x average
- On-chain: >$100k transfers

---

### 3. 📰 News Scanner (`news.py`)

**Purpose**: Multi-source news aggregation with credibility scoring

**Signals Emitted**:
- `news_event` - General news detected
- `news_bullish` - Positive news sentiment
- `news_bearish` - Negative news sentiment

**Data Sources**: 
- CoinPaprika news search
- NewsAPI (if `NEWSAPI_KEY` set)

**Credibility Scoring**: Based on source reputation and article freshness

---

### 4. 💭 Sentiment Agent (`sentiment.py`)

**Purpose**: Aggregates Reddit + Twitter + news sentiment per coin

**Signals Emitted**:
- `sentiment_shot` - Bullish sentiment surge
- `sentiment_bear` - Bearish sentiment detected

**Data Sources**: Reddit API, Twitter API, news sources

**Configuration**:
- `SENTIMENT_WINDOW_DAYS = 3`
- `SENTIMENT_MIN_SOURCES = 3`
- `SENTIMENT_CONFIDENCE_THRESHOLD = 0.55`

---

### 5. 📊 Pattern Recognition (`pattern.py`)

**Purpose**: Detects technical patterns (double-bottom, head-and-shoulders, breakouts)

**Signals Emitted**:
- `pattern_bullish` - Bullish pattern detected
- `pattern_bearish` - Bearish pattern detected

**Patterns Detected**:
- Double bottom
- Head and shoulders
- Volume breakouts
- Support/resistance breaks

**Configuration**:
- `PATTERN_MIN_CONFIDENCE = 0.60`
- `PATTERN_LOOKBACK_DAYS = 90`
- `PATTERN_VOL_MULTIPLIER = 1.5`

---

### 6. 🔗 Correlation Monitor (`correlation.py`)

**Purpose**: Watches BTC/alt correlation spikes and dumps

**Signals Emitted**:
- `correlation_spike` - High positive correlation with BTC
- `correlation_dump` - High negative correlation (alt bleeding)

**Configuration**:
- `CORRELATION_WINDOW = 30` days
- `CORRELATION_SPIKE_THRESHOLD = 0.8`
- `CORRELATION_DUMP_THRESHOLD = -0.7`

---

### 7. 🛡️ Due Diligence (`dd.py`)

**Purpose**: Verifies liquidity, holder concentration, and market traction

**Signals Emitted**:
- `dd_result` - Overall DD score
- `liquidity_health` - Liquidity check
- `holder_concentration` - Holder distribution
- `volume_depth` - Volume sustainability
- `age_survivorship` - Project age
- `market_traction` - Market presence

**DD Score Weights**:
```python
DD_WEIGHTS = {
    "liquidity_health": 0.25,
    "holder_concentration": 0.25,
    "volume_depth": 0.20,
    "age_survivorship": 0.15,
    "market_traction": 0.15,
}
```

**Thresholds**:
- `DD_BUY_THRESHOLD = 0.4` - Minimum score for BUY
- `HOLDER_RED_FLAG_CUT = 0.30` - Max holder concentration

---

### 8. ⛓️ On-Chain Holder (`onchain.py`)

**Purpose**: Checks token holder concentration via Etherscan

**Signals Emitted**:
- `holder_red_flag` - Centralized holdings (>30%)
- `holder_healthy` - Distributed holdings

**Data Sources**: Etherscan API (requires `ETHERSCAN_API_KEY`)

**Risk Threshold**: >30% held by top 10 wallets = red flag

---

### 9. 🌐 Macro Regime (`macro.py`)

**Purpose**: Determines market regime using Fear & Greed + BTC dominance

**Signals Emitted**:
- `regime_risk_on` - Bullish macro
- `regime_neutral` - Neutral macro
- `regime_risk_off` - Bearish macro

**Data Sources**: Alternative.me Fear & Greed Index, CoinGecko Global

**Thresholds**:
- Risk ON: F&G > 60, BTC Dom < 55%
- Risk OFF: F&G < 30, BTC Dom > 60%
- Neutral: Otherwise

---

### 10. 🧠 Meta-Learner (`meta_learner.py`)

**Purpose**: Tracks agent accuracy, adjusts weights, detects regime shifts

**Signals Emitted**:
- `meta_agent_trusted` - Agent is performing well
- `meta_agent_deprioritized` - Agent underperforming

**Functions**:
- Per-agent win rate tracking
- Signal weight adjustment
- Regime shift detection
- Overfit signal detection

**Configuration**:
- `META_MIN_SAMPLES = 5` - Minimum predictions before adjustment
- `META_REGIME_SWITCH_THRESHOLD = 0.15`
- `META_AGENT_PENALTY = 0.05` - Penalty for underperformers

---

### 11. 🎯 Investment Advisor (`advisor.py`)

**Purpose**: Consolidates all signals into final BUY/WATCH/AVOID verdicts

**Output**: Ranked list of coins with:
- Action: BUY / HOLD / WATCH / AVOID
- Score: Weighted signal score
- DD: Due diligence score
- Reasoning: Signal breakdown
- Risk notes: From risk manager

**Scoring Logic**:
```python
BUY_SCORE_THRESHOLD = 0.15
# Score = Σ(signal_weight × confidence × regime_multiplier)
```

---

## 🌐 API Endpoints

### Dashboard Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main dashboard UI |
| `/health` | GET | Health check (for Render) |
| `/api/status` | GET | Scan status (running, last_run, errors) |
| `/api/scan` | POST | Trigger background scan |
| `/api/report` | GET | Latest report + verdict cards |
| `/api/signals` | GET | Signal bus summary |
| `/api/attribution` | GET | P&L attribution by agent |
| `/api/agents` | GET | Agent roster + memory snapshots |

### Coin Data Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/coin/<symbol>` | GET | Coin details (price, change, market cap) |
| `/api/chart/<symbol>?interval=7d` | GET | Price chart data (line series) |
| `/api/ohlc/<symbol>?interval=1d` | GET | OHLC candlestick data |

**Intervals**: `1d`, `7d`, `30d`, `90d`, `1y`

---

## 🚀 Deployment Workflow

### Render.com Deployment (Free Tier)

#### Method 1: Auto-Deploy via GitHub

1. **Connect Repository to Render**
   ```
   Dashboard → New Web Service → Connect GitHub repo
   ```

2. **Configure Service** (auto-detected from `render.yaml`)
   - **Name**: `super-crypto-agent`
   - **Runtime**: Python 3.11
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn server:app`
   - **Plan**: Free (750 hours/month)

3. **Set Environment Variables**
   ```
   COINGECKO_API_KEY=your_key_here  (optional)
   ETHERSCAN_API_KEY=your_key_here  (optional)
   NEWSAPI_KEY=your_key_here        (optional)
   ```

4. **Deploy**
   - Auto-deploys on push to `main` branch
   - Health check at `/health`
   - Live URL: `https://super-crypto-agent.onrender.com`

#### Method 2: Render CLI

```bash
# Install Render CLI
npm install -g @render/cli

# Deploy
render deploy --service-name super-crypto-agent
```

---

## 💻 Development Workflow

### Local Development

```bash
# 1. Create a feature branch
git checkout -b feature/new-agent

# 2. Make changes
# - Add new agent to supercrypto/agents/
# - Register signals in supercrypto/config.py (ALL_SIGNALS)
# - Import in run_pipeline.py
# - Add to agent execution sequence

# 3. Test locally
python3 run_pipeline.py --quick
python3 server.py

# 4. Run tests
python3 tests/test_supercrypto.py
python3 tests/test_alphaforge.py

# 5. Commit and push
git add .
git commit -m "feat: add new XYZ agent"
git push origin feature/new-agent

# 6. Create Pull Request
# GitHub → Compare & pull request
```

### Adding a New Agent

```python
# 1. Create supercrypto/agents/new_agent.py
from supercrypto.core.base import BaseAgent

class NewAgent(BaseAgent):
    def __init__(self):
        super().__init__("NewAgent")
    
    def execute(self):
        # Agent logic here
        self.emit("new_signal", coin="BTC", confidence=0.8)

# 2. Register signals in config.py
ALL_SIGNALS = frozenset({
    # ... existing signals ...
    "new_signal",
})

# 3. Add to run_pipeline.py
from supercrypto.agents.new_agent import NewAgent

def run_once(args):
    # ... existing agents ...
    print("[X] new agent")
    NewAgent().execute()
```

### Signal Bus Pattern

All agents emit signals to a shared `data/signals.json` bus:

```python
# Emitting a signal
self.emit(
    signal="whale_up",
    coin="BTC",
    confidence=0.85,
    reason="24h volume spike +120%"
)

# Reading from bus
with open(SIGNALS_FILE) as f:
    signals = json.load(f)
    
# Filtering signals
buy_signals = [s for s in signals 
               if s["coin"] == "BTC" 
               and s["confidence"] > 0.7]
```

---

## 🧪 Testing Strategy

### Test Files

1. **`tests/test_supercrypto.py`** (9 tests)
   - Enhanced feature tests
   - Regime-aware learning
   - Cross-agent knowledge sharing
   - Multi-dimensional rewards

2. **`tests/test_alphaforge.py`** (25 tests)
   - Original core functionality
   - Signal emission
   - Weight learning
   - Risk management

### Running Tests

```bash
# Run all tests
python3 tests/test_supercrypto.py
python3 tests/test_alphaforge.py

# Expected output:
# test_supercrypto.py: 9 tests pass
# test_alphaforge.py: 25 tests pass
```

### Test Coverage Areas

- ✅ Agent signal emission
- ✅ Weight learning (EMA updates)
- ✅ Regime detection (risk_on/neutral/risk_off)
- ✅ Cross-agent knowledge sharing
- ✅ Risk management (position sizing, circuit breakers)
- ✅ Paper trading (equity tracking)
- ✅ Attribution engine (P&L decomposition)
- ✅ Due diligence scoring
- ✅ Meta-learner adjustments

---

## 📊 Data Flow

### Signal Flow

```
Market APIs (CoinGecko, Binance, etc.)
    ↓
Agents (10 specialized agents)
    ↓
signals.json (signal bus)
    ↓
WeightLearner (regime-aware EMA)
    ↓
Investment Advisor (verdict generation)
    ↓
Risk Manager (position sizing)
    ↓
Paper Trader (equity tracking)
    ↓
Attribution Engine (P&L decomposition)
    ↓
Reports + Dashboard
```

### Memory Persistence

Each agent maintains a persistent memory file:

```
data/memory/
├── whale_memory.json
├── sentiment_memory.json
├── pattern_memory.json
├── news_memory.json
├── dd_memory.json
├── macro_memory.json
├── meta_memory.json
└── advisor_memory.json
```

**Memory Contains**:
- `runs` - Total execution count
- `weights` - Signal weight dict
- `regime_weights` - Per-regime weights
- `predictions` - Historical predictions
- `agent_stats` - Per-agent performance

---

## ⚙️ Configuration

### Key Configuration Files

#### `supercrypto/config.py`

**Paths**:
```python
DATA_DIR = "data/"
SIGNALS_FILE = "data/signals.json"
MEMORY_DIR = "data/memory/"
REPORTS_DIR = "data/reports/"
WATCHLIST_FILE = "data/watchlist.json"
PAPER_FILE = "data/paper_portfolio.json"
```

**Timezone**:
```python
JST = timezone(timedelta(hours=9), "JST")
# All scan times displayed in Japan Standard Time
```

**Learning Parameters**:
```python
EMA_ALPHA = 0.2                    # Weight learning rate
MIN_WEIGHT = 0.05                  # Min signal weight
MAX_WEIGHT = 1.0                   # Max signal weight
MIN_SAMPLES = 3                    # Min samples before weight adjustment
HORIZON_WEIGHTS = {
    "24h": 0.6,                    # 24h horizon weight
    "7d": 0.4                      # 7d horizon weight
}
DEFAULT_SIGNAL_WEIGHT = 0.5        # Initial signal weight
```

**Risk Management**:
```python
DEFAULT_STOP_LOSS_PCT = 15.0       # Stop loss percentage
DEFAULT_TAKE_PROFIT_PCT = 45.0     # Take profit percentage
DEFAULT_MAX_POSITION_PCT = 5.0     # Max position size
DEFAULT_MAX_TOTAL_EXPOSURE_PCT = 20.0  # Max total exposure
DEFAULT_MAX_CONCURRENT_BUYS = 5    # Max concurrent positions
DEFAULT_CIRCUIT_BREAKER_DD_PCT = 25.0  # Circuit breaker threshold
STARTING_EQUITY = 100.0            # Paper trading starting equity
```

**Regime Detection**:
```python
FNG_RISK_ON = 60                   # Fear & Greed > 60 = risk on
FNG_RISK_OFF = 30                  # Fear & Greed < 30 = risk off
BTC_DOM_RISK_OFF = 60.0            # BTC dominance > 60% = risk off
BTC_DOM_RISK_ON_MAX = 55.0         # BTC dominance < 55% = risk on
```

**Thresholds**:
```python
DD_BUY_THRESHOLD = 0.4             # Min DD score for BUY
BUY_SCORE_THRESHOLD = 0.15         # Min score for BUY verdict
HOLDER_RED_FLAG_CUT = 0.30         # Max holder concentration
```

---

## 🔧 Troubleshooting

### Common Issues

#### 1. Rate Limit Errors (CoinGecko)

**Problem**: Too many API requests

**Solution**:
```bash
# Set API key for higher limits
export COINGECKO_API_KEY="your_key_here"

# Use --quick flag to skip slow agents
python3 run_pipeline.py --quick
```

#### 2. Missing On-Chain Data

**Problem**: On-chain agents skipped

**Solution**:
```bash
# Set Etherscan API key
export ETHERSCAN_API_KEY="your_key_here"
```

#### 3. Empty Reports

**Problem**: No verdicts generated

**Solution**:
- Check `data/signals.json` has signals
- Verify agents executed successfully
- Lower `BUY_SCORE_THRESHOLD` in config.py
- Check `DD_BUY_THRESHOLD` not too high

#### 4. Dashboard Not Loading

**Problem**: Server won't start or charts missing

**Solution**:
```bash
# Check port availability
lsof -i :8080

# Verify dependencies
pip install -r requirements.txt

# Check logs
python3 server.py --host 0.0.0.0 --port 8080
```

---

## 📈 Performance Optimization

### Cache Warming

The server pre-warms caches on startup:

1. **Market Cache** - Top 250 coins from CoinGecko
2. **Microcap Cache** - Batched fetch for report coins
3. **Chart Cache** - Parallel 7D chart fetching

**Cache Files**:
- `data/market_cache.json` - Persists across restarts

### Quick Mode

Skip slow agents for faster scans:

```bash
python3 run_pipeline.py --quick
```

**Skipped in Quick Mode**:
- Sentiment analysis (Reddit/Twitter scraping)
- Pattern recognition (90-day chart analysis)
- Correlation monitoring (30-day correlation matrix)

**Quick Mode Runtime**: ~30-60 seconds (vs 2-5 minutes full scan)

---

## 📝 Output Examples

### Console Output

```
super-crypto-agent — multi-agent signal & research engine (paper-trading only)
[0] macro regime
    regime: risk_on
[1-d] discovery
    done in 12.3s
[2] due diligence
[2b] on-chain holders
[2c] whale on-chain transfers
[3] meta-learner
[4] advisor
[5] risk management
[6] paper trading
    equity 105.2 | open 3 | closed 2 | win 50% | maxDD 3.2%
bus: 47 signals - {'whale': 12, 'news': 8, 'dd': 15, 'macro': 3, 'advisor': 9}
top verdicts:
  🟢 BTC        BUY    score=+0.234 dd=0.82
  🟢 ETH        BUY    score=+0.187 dd=0.79
  🟡 SOL        WATCH  score=+0.112 dd=0.65
  🟡 AVAX       WATCH  score=+0.098 dd=0.71
  🔴 SHIB       AVOID  score=-0.045 dd=0.23 [risk: holder_concentration]
done in 18.7s - reports in data/reports/
```

### Report Output

**Location**: `data/reports/report_YYYYMMDD_HHMMSS.md`

```markdown
# Super Crypto Agent — Market Intelligence Report

Generated: 2026-10-02 17:23:45 JST
Regime: risk_on

## 🟢 BTC — BUY

- **Score**: +0.234 (high confidence)
- **DD**: 0.82/1.0 (strong fundamentals)
- Whale activity detected (+15% volume spike)
- Bullish news: "BTC ETF approval rumors"
- Sentiment: 0.78 (very bullish)
- Technical: Double-bottom pattern confirmed

## 🟢 ETH — BUY

- **Score**: +0.187 (high confidence)
- **DD**: 0.79/1.0 (strong fundamentals)
- Correlation spike with BTC (0.85)
- News: "Ethereum Shanghai upgrade success"
- On-chain: Healthy holder distribution (12% top 10)

## 🟡 SOL — WATCH

- **Score**: +0.112 (moderate)
- **DD**: 0.65/1.0 (adequate)
- Micro-cap momentum (volume up 80%)
- Wait for confirmation before entry

## 🔴 SHIB — AVOID

- **Score**: -0.045 (weak)
- **DD**: 0.23/1.0 (poor fundamentals)
- **Risk**: Holder concentration >30% (red flag)
- Low liquidity, high manipulation risk
```

---

## 🎯 Best Practices

### For Production Use

1. **Set API Keys** for full functionality
2. **Use Quick Mode** on cloud/free tiers to avoid rate limits
3. **Monitor Circuit Breakers** - halts trading at 25% drawdown
4. **Review Attribution** regularly to identify best-performing agents
5. **Adjust Thresholds** based on market conditions
6. **Backup Memory Files** to preserve learning

### For Development

1. **Test Locally First** before pushing to production
2. **Use `--no-clear` Flag** to preserve signal bus for debugging
3. **Check Agent Memory** in `data/memory/` to verify learning
4. **Run Tests** after any agent changes
5. **Monitor Logs** for API errors and rate limits

---

## 🔮 Future Enhancements

Potential areas for expansion:

1. **Real Trading Integration** (currently paper-only)
2. **More Data Sources** (Messari, Glassnode, Santiment)
3. **Machine Learning Models** for pattern recognition
4. **Social Media Sentiment** (full Twitter/Reddit integration)
5. **Webhook Notifications** (Discord, Telegram, Slack)
6. **Portfolio Optimization** (Kelly criterion, risk parity)
7. **Multi-Exchange Support** (Binance, Coinbase, Kraken)
8. **Historical Backtesting** with full portfolio simulation

---

## 📞 Support & Contributing

- **Repository**: https://github.com/mattappsaibagus-wq/super-crypto-agent
- **Issues**: File via GitHub Issues
- **Pull Requests**: Welcome for bug fixes and enhancements

### Contribution Guidelines

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Write tests for new functionality
4. Ensure all tests pass
5. Commit with descriptive messages
6. Push to your fork
7. Open a Pull Request

---

## ⚠️ Disclaimer

This tool provides market intelligence signals for **informational and educational purposes only**. It is a personal research tool — **not financial advice**. Cryptocurrency markets are highly volatile; you may lose some or all of your invested capital. Past performance does not indicate future results. You are solely responsible for your investment decisions.

---

**Document Version**: 1.0  
**Last Updated**: 2026-10-02  
**Maintained By**: Repository Contributors
