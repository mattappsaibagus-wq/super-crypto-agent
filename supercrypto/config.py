"""All tunables in one place for Super Crypto Agent.

Extends alpha-forge config with regime-aware learning, cross-agent knowledge
sharing, and new agent types (sentiment, pattern, correlation, meta-learner).
"""

import os
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
PAPER_FILE = os.path.join(DATA_DIR, "paper_portfolio.json")
VERDICTS_FILE = os.path.join(DATA_DIR, "verdicts.json")

# All user-facing scan times are shown in Japan Standard Time. Japan has no
# DST, so a fixed +09:00 offset is exact and needs no tzdata package. The
# server itself (e.g. Render) runs in UTC, so never use naive datetime.now()
# for anything a user will see.
JST = timezone(timedelta(hours=9), "JST")


def now_jst():
    """Current time as a timezone-aware datetime in Japan Standard Time."""
    return datetime.now(JST)

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
SANTIMENT_GRAPHQL = "https://api.santiment.net/graphql"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
FUNDRAISING_DEALFLOW = "https://crypto-fundraising.info/deal-flow/"

KNOWN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin", "SOL": "solana",
    "TRX": "tron", "DOT": "polkadot", "LINK": "chainlink",
    "MATIC": "polygon", "LTC": "litecoin", "AVAX": "avalanche-2",
    "UNI": "uniswap", "XLM": "stellar", "ATOM": "cosmos",
    "FIL": "filecoin", "NEAR": "near", "ARB": "arbitrum", "OP": "optimism",
    "BCH": "bitcoin-cash", "SUI": "sui", "TON": "the-open-network",
    "SHIB": "shiba-inu", "PEPE": "pepe", "PENGU": "pudgy-penguins",
    "ENA": "ethena", "HNT": "helium",
    "USDT": "tether", "USDC": "usd-coin", "DAI": "dai",
    "WIF": "dogwifcoin", "JUP": "jupiter-exchange-solana",
    # Microcap coins that appear in verdicts (resolved via coin_master_list fallback)
    "PRL": "pearl-2", "EDEL": "edel", "SUIT": "dog-in-a-suit",
    "USELESS": "useless-3", "STONK": "stonk-3", "SONIC": "sonic-1",
    "TAO": "bittensor", "ZEC": "zcash", "SUSHI": "sushi",
    "FTM": "fantom", "HBAR": "hedera", "SEI": "sei-2", "PYTH": "pyth-network",
    "STRK": "starknet-2", "KAS": "kaspa", "RNDR": "render-token",
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
    # Santiment on-chain / dev / social activity
    "dev_activity_up", "active_addresses_spike", "active_addresses_fade", "social_spike",
    # Hyperliquid perp positioning (the data Buildix visualises)
    "funding_squeeze", "oi_buildup", "funding_overheated", "oi_flush",
    # VC funding rounds (crypto-fundraising.info)
    "fresh_funding",
    # Kronos foundation-model candlestick forecasts
    "kronos_forecast_up", "kronos_forecast_down",
})

# Signals the advisor subtracts from a coin's bias instead of adding.
BEARISH_SIGNALS = frozenset({
    "whale_down", "whale_onchain_down", "news_bearish",
    "sentiment_bear", "pattern_bearish",
    "active_addresses_fade", "funding_overheated", "oi_flush",
    "kronos_forecast_down",
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

# Santiment (free metrics work without a key; a key unlocks more + higher limits)
SANTIMENT_MAX_SLUGS = 25          # coins per scan; all fetched in one call per metric
SANTIMENT_MONTHLY_CALL_CAP = 900  # free SanAPI plan is 1,000 calls/month
SANTIMENT_DEV_RATIO = 1.5         # last-7d dev activity vs prior 3-week weekly average
SANTIMENT_DAA_SPIKE = 2.0         # daily active addresses vs prior 14d median
SANTIMENT_DAA_FADE = 0.5
SANTIMENT_SOCIAL_SPIKE = 3.0

# Hyperliquid derivatives
HL_MIN_OI_USD = 1_000_000
HL_MIN_DAY_VOLUME_USD = 1_000_000
HL_FUNDING_HOT = 0.0001           # hourly; ~88% APR paid by longs → crowded
HL_FUNDING_SQUEEZE = -0.00005     # hourly; shorts paying ~44% APR
HL_OI_BUILDUP_PCT = 20.0
HL_OI_FLUSH_PCT = -25.0
HL_MAX_SIGNALS = 15

# Fundraising
FUNDRAISING_FRESH_DAYS = 14       # signal a deal for this long after first seen

# Kronos forecast agent (vendor/kronos, optional deps in requirements-kronos.txt)
KRONOS_MODEL_ID = "NeoQuasar/Kronos-small"
KRONOS_TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
KRONOS_INTERVAL_HOURS = 4         # 4h candles
KRONOS_LOOKBACK = 360             # bars of context (60 days; model max is 512)
KRONOS_MIN_HISTORY = 180          # skip coins with less than 30 days of 4h bars
KRONOS_PRED_LEN = 6               # 6 x 4h = 24h, the horizon outcomes settle at
KRONOS_N_PATHS = 8                # independent sampled paths per coin
KRONOS_MAX_COINS = 30             # coins forecast per scan
KRONOS_MIN_MOVE_PCT = 1.0         # |expected 24h move| noise floor
KRONOS_MIN_AGREEMENT = 0.75       # share of paths that must agree on direction
KRONOS_MIN_T_STAT = 2.5           # mean move / std error across paths
KRONOS_MAX_MOVE_VS_VOL = 4.0      # forecasts > 4x daily volatility are shown, never signalled
KRONOS_FORECASTS_FILE = os.path.join(DATA_DIR, "kronos_forecasts.json")
KRONOS_HISTORY_KEEP = 600         # graded forecasts kept for the track record

# Optional API keys (set via environment variables)
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "").strip()
SANTIMENT_API_KEY = os.environ.get("SANTIMENT_API_KEY", "").strip()
