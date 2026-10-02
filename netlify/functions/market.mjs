// Live market data for the dashboard (port of /api/coin, /api/chart,
// /api/ohlc and /api/scan from server.py) running as a Netlify Function.
// Ticker -> CoinGecko id (mirrors KNOWN_IDS in supercrypto/config.py)
const CG_IDS = {"ADA": "cardano", "ARB": "arbitrum", "ATOM": "cosmos", "AVAX": "avalanche-2", "BCH": "bitcoin-cash", "BNB": "binancecoin", "BTC": "bitcoin", "DAI": "dai", "DOGE": "dogecoin", "DOT": "polkadot", "EDEL": "edel", "ENA": "ethena", "ETH": "ethereum", "FIL": "filecoin", "FTM": "fantom", "HBAR": "hedera", "HNT": "helium", "JUP": "jupiter-exchange-solana", "KAS": "kaspa", "LINK": "chainlink", "LTC": "litecoin", "MATIC": "polygon", "NEAR": "near", "OP": "optimism", "PENGU": "pudgy-penguins", "PEPE": "pepe", "PRL": "pearl-2", "PYTH": "pyth-network", "RNDR": "render-token", "SEI": "sei-2", "SHIB": "shiba-inu", "SOL": "solana", "SONIC": "sonic-1", "STONK": "stonk-3", "STRK": "starknet-2", "SUI": "sui", "SUIT": "dog-in-a-suit", "SUSHI": "sushi", "TAO": "bittensor", "TON": "the-open-network", "TRX": "tron", "UNI": "uniswap", "USDC": "usd-coin", "USDT": "tether", "USELESS": "useless-3", "WIF": "dogwifcoin", "XLM": "stellar", "XRP": "ripple", "ZEC": "zcash"};

const COINGECKO = "https://api.coingecko.com/api/v3";
const BINANCE = "https://data-api.binance.vision/api/v3";
const CG_KEY = (process.env.COINGECKO_API_KEY || "").trim();

const BN_PAIRS = {
  BTC: "BTCUSDT", ETH: "ETHUSDT", BNB: "BNBUSDT", SOL: "SOLUSDT", XRP: "XRPUSDT",
  ADA: "ADAUSDT", DOGE: "DOGEUSDT", AVAX: "AVAXUSDT", LINK: "LINKUSDT", DOT: "DOTUSDT",
  MATIC: "POLUSDT", POL: "POLUSDT", LTC: "LTCUSDT", NEAR: "NEARUSDT", APT: "APTUSDT",
  ARB: "ARBUSDT", OP: "OPUSDT", SUI: "SUIUSDT", UNI: "UNIUSDT", AAVE: "AAVEUSDT",
  MKR: "MKRUSDT", PEPE: "PEPEUSDT", SHIB: "SHIBUSDT", BONK: "BONKUSDT", TRX: "TRXUSDT",
  FIL: "FILUSDT", ATOM: "ATOMUSDT", INJ: "INJUSDT", ZEC: "ZECUSDT", PENGU: "PENGUUSDT",
  ETC: "ETCUSDT", TAO: "TAOUSDT", SUSHI: "SUSHIUSDT", FTM: "FTMUSDT", HBAR: "HBARUSDT",
  SEI: "SEIUSDT", IMX: "IMXUSDT", THETA: "THETAUSDT", CFX: "CFXUSDT", KAS: "KASUSDT",
  QNT: "QNTUSDT", TIA: "TIAUSDT", STRK: "STRKUSDT", WLD: "WLDUSDT", PYTH: "PYTHUSDT",
  JUP: "JUPUSDT", WIF: "WIFUSDT", ICP: "ICPUSDT", RNDR: "RNDRUSDT", SNX: "SNXUSDT",
  GRT: "GRTUSDT", CRV: "CRVUSDT", LIT: "LITUSDT", STX: "STXUSDT", KSM: "KSMUSDT",
};
const INTERVAL_BN = { "1d": "30m", "7d": "1h", "30d": "1d", "90d": "1d", "1y": "1d" };
const INTERVAL_DAYS = { "1d": 1, "7d": 7, "30d": 30, "90d": 90, "1y": 365 };

const json = (body, status = 200, maxAge = 60) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json",
      "cache-control": `public, max-age=${maxAge}`,
      "netlify-cdn-cache-control": `public, s-maxage=${maxAge}, stale-while-revalidate=${maxAge}`,
    },
  });

async function apiGet(url, params = {}, tries = 2) {
  const u = new URL(url);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, String(v));
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(u, { headers: { accept: "application/json" }, signal: AbortSignal.timeout(8000) });
      if (r.ok) return await r.json();
      if (r.status !== 429 && r.status < 500) return null;
    } catch { /* retry */ }
    await new Promise((res) => setTimeout(res, 600 * (i + 1)));
  }
  return null;
}

const cgParams = (p) => (CG_KEY ? { ...p, x_cg_demo_api_key: CG_KEY } : p);
const pairFor = (s) => BN_PAIRS[s] || `${s}USDT`;

async function cgIdFor(symbol) {
  if (CG_IDS[symbol]) return CG_IDS[symbol];
  const res = await apiGet(`${COINGECKO}/search`, cgParams({ query: symbol }), 1);
  const hit = res?.coins?.find((c) => (c.symbol || "").toUpperCase() === symbol);
  return hit?.id || symbol.toLowerCase();
}

async function binanceKlines(symbol, interval) {
  const data = await apiGet(`${BINANCE}/klines`, {
    symbol: pairFor(symbol), interval: INTERVAL_BN[interval] || "30m", limit: 500,
  });
  return Array.isArray(data) ? data : [];
}

async function coin(symbol) {
  const cid = await cgIdFor(symbol);
  const data = await apiGet(`${COINGECKO}/coins/${cid}`, cgParams({
    localization: "false", tickers: "false", community_data: "false",
    developer_data: "false", sparkline: "false",
  }));
  if (data?.market_data) {
    const md = data.market_data;
    return json({
      symbol: (data.symbol || "").toUpperCase(),
      name: data.name || "",
      image: data.image?.large || "",
      price: md.current_price?.usd ?? null,
      change_24h: md.price_change_percentage_24h ?? null,
      change_7d: md.price_change_percentage_7d ?? md.price_change_percentage_7d_in_currency?.usd ?? null,
      change_30d: md.price_change_percentage_30d ?? md.price_change_percentage_30d_in_currency?.usd ?? null,
      ath: md.ath?.usd ?? null,
      ath_change: md.ath_change_percentage?.usd ?? null,
      market_cap: md.market_cap?.usd ?? null,
      volume_24h: md.total_volume?.usd ?? null,
      circulating_supply: md.circulating_supply ?? null,
      total_supply: md.total_supply ?? null,
      source: "coingecko",
    }, 200, 300);
  }
  const bn = await apiGet(`${BINANCE}/ticker/24hr`, { symbol: pairFor(symbol) }, 1);
  if (bn?.lastPrice) {
    const qv = parseFloat(bn.quoteVolume || 0);
    return json({
      symbol, name: symbol, image: "",
      price: parseFloat(bn.lastPrice), change_24h: parseFloat(bn.priceChangePercent || 0),
      change_7d: null, ath: null, ath_change: null,
      market_cap: qv * 100, volume_24h: qv,
      circulating_supply: null, total_supply: null, source: "binance",
    }, 200, 300);
  }
  return json({ error: "coin not found" }, 200, 120);
}

async function chart(symbol, interval) {
  const days = INTERVAL_DAYS[interval] || 1;
  const kl = await binanceKlines(symbol, interval);
  if (kl.length >= 2) {
    return json({
      symbol, interval, days, source: "binance",
      prices: kl.map((k) => ({ t: k[0], price: parseFloat(k[4]) })),
    }, 200, 600);
  }
  const cid = await cgIdFor(symbol);
  const p = { vs_currency: "usd", days };
  if (days >= 2) p.interval = "daily";
  const data = await apiGet(`${COINGECKO}/coins/${cid}/market_chart`, cgParams(p), 3);
  if (data?.prices) {
    return json({
      symbol, interval, days, source: "coingecko",
      prices: data.prices.filter((x) => x[1] > 0).map((x) => ({ t: x[0], price: Math.round(x[1] * 1e4) / 1e4 })),
    }, 200, 600);
  }
  return json({ error: "chart data unavailable", symbol }, 404, 60);
}

async function ohlc(symbol, interval) {
  if (!INTERVAL_BN[interval]) interval = "1d";
  const ttl = interval === "1d" ? 180 : 900;
  const kl = await binanceKlines(symbol, interval);
  if (kl.length >= 2) {
    return json({
      symbol, interval, source: "binance",
      candles: kl.map((k) => ({ t: k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4], v: +k[5] })),
    }, 200, ttl);
  }
  const cid = await cgIdFor(symbol);
  const data = await apiGet(`${COINGECKO}/coins/${cid}/ohlc`, cgParams({ vs_currency: "usd", days: INTERVAL_DAYS[interval] || 1 }));
  if (Array.isArray(data) && data.length) {
    return json({
      symbol, interval, source: "coingecko",
      candles: data.filter((r) => Array.isArray(r) && r.length >= 5)
        .map((r) => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: null })),
    }, 200, ttl);
  }
  return json({ error: "OHLC data unavailable", symbol }, 404, 60);
}

export default async (req, context) => {
  const url = new URL(req.url);
  const [, , kind, raw] = url.pathname.split("/"); // /api/<kind>/<symbol>
  if (kind === "scan") {
    return json({ status: "scheduled", message: "Scans run every 6h via GitHub Actions" }, 200, 0);
  }
  const symbol = decodeURIComponent(context.params?.symbol || raw || "").toUpperCase();
  if (!symbol) return json({ error: "symbol required" }, 400, 0);
  const interval = url.searchParams.get("interval") || "1d";
  if (kind === "coin") return coin(symbol);
  if (kind === "chart") return chart(symbol, interval);
  if (kind === "ohlc") return ohlc(symbol, interval);
  return json({ error: "not found" }, 404, 0);
};

export const config = {
  path: ["/api/coin/:symbol", "/api/chart/:symbol", "/api/ohlc/:symbol", "/api/scan"],
};
