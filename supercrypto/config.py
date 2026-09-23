"""All tunables in one place for Super Crypto Agent.

Extends alpha-forge config with regime-aware learning, cross-agent knowledge
sharing, and new agent types (sentiment, pattern, correlation, meta-learner).
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
PAPER_FILE = os.path.join(DATA_DIR, "paper_portfolio.json")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/token"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/24hr"
FEAR_GREED_URL = "https://api.alternative.me/fng/"
COINPAPRIKA_SEARCH = "https://api.coinpaprika.com/v1/search"
COINPAPRIKA_TRENDING = "https://api.coinpaprika.com/v1/search/trending"
ETHERSCAN_API = "https://api.etherscan.io/api"
TWITTER_API_BASE = "https://api.twitterapi.xyz"
REDDIT_API_BASE = "https://www.reddit.com/r"
NEWSAPI_BASE = "https://newsapi.org/v2/everything"

KNOWN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin", "SOL": "solana",
    "TRX": "tron", "DOT": "polkadot", "LINK": "chainlink",
    "MATIC": "matic-network", "LTC": "litecoin", "AVAX": "avalanche-2",
    "UNI": "uniswap", "XLM": "stellar", "ATOM": "cosmos",
    "FIL": "filecoin", "NEAR": "near", "ARB": "arbitrum", "OP": "optimism",
    "BCH": "bitcoin-cash", "SUI": "sui", "TON": "the-open-network",
    "SHIB": "shiba-inu", "PEPE": "pepe", "PENGU": "pudgy-penguins",
    "ENA": "ethena", "HNT": "helium",
    "USDT": "tether", "USDC": "usd-coin", "DAI": "dai",
    "WIF": "dogwifcoin", "JUP": "jupiter-exchange-solana",
}

ALL_SIGNALS = frozenset({
    "microcap_opportunity",
    "strong_volume_ratio", "low_liquidity_risk", "age_signal",
    "trending_boost", "dex_listing",
    "whale_up", "whale_down",
    "whale_onchain", "whale_onchain_down",
    "volume_spike", "price_move", "momentum", "liquidity_depth", "sustainability",
    "news_event", "news_bullish", "news_bearish",
    "sentiment_shot", "sentiment_bear",
    "pattern_bullish", "pattern_bearish",
    "dd_result",
    "liquidity_health", "holder_concentration", "volume_depth",
    "age_survivorship", "market_traction",
    "correlation_spike", "correlation_dump",
    "advisor_buy", "advisor_watch", "advisor_avoid", "advisor_hold",
    "regime_risk_on", "regime_neutral", "regime_risk_off",
    "holder_red_flag", "holder_healthy",
    "meta_agent_trusted", "meta_agent_deprioritized",
})

DD_WEIGHTS = {
    "liquidity_health": 0.25,
    "holder_concentration": 0.25,
    "volume_depth": 0.20,
    "age_survivorship": 0.15,
    "market_traction": 0.15,
}

EMA_ALPHA = 0.2
MIN_WEIGHT = 0.05
MAX_WEIGHT = 1.0
MIN_SAMPLES = 3
HORIZON_WEIGHTS = {"24h": 0.6, "7d": 0.4}
DEFAULT_SIGNAL_WEIGHT = 0.5

DD_BUY_THRESHOLD = 0.4
BUY_SCORE_THRESHOLD = 0.15
HOLDER_RED_FLAG_CUT = 0.30

DEFAULT_STOP_LOSS_PCT = 15.0
DEFAULT_TAKE_PROFIT_PCT = 45.0
DEFAULT_MAX_POSITION_PCT = 5.0
DEFAULT_MAX_TOTAL_EXPOSURE_PCT = 20.0
DEFAULT_MAX_CONCURRENT_BUYS = 5
DEFAULT_CIRCUIT_BREAKER_DD_PCT = 25.0
STARTING_EQUITY = 100.0

FNG_RISK_ON = 60
FNG_RISK_OFF = 30
BTC_DOM_RISK_OFF = 60.0
BTC_DOM_RISK_ON_MAX = 55.0

# Sentiment scoring
SENTIMENT_WINDOW_DAYS = 3
SENTIMENT_MIN_SOURCES = 3
SENTIMENT_CONFIDENCE_THRESHOLD = 0.55
SENTIMENT_REDDIT_LIMIT = 100

# Pattern recognition
PATTERN_MIN_CONFIDENCE = 0.60
PATTERN_LOOKBACK_DAYS = 90
PATTERN_VOL_MULTIPLIER = 1.5

# Correlation monitor
CORRELATION_WINDOW = 30
CORRELATION_SPIKE_THRESHOLD = 0.8
CORRELATION_DUMP_THRESHOLD = -0.7

# Meta-learner
META_MIN_SAMPLES = 5
META_REGIME_SWITCH_THRESHOLD = 0.15
META_AGENT_PENALTY = 0.05

# On-chain
DEFAULT_TOP_N_COINS = 200
DEFAULT_VOL_THRESHOLD = 50_000
DEFAULT_MCAP_MAX = 50_000_000

API_TRIES = 2
API_TIMEOUT = 10
