"""PURE scoring functions shared by live agents AND the backtester.

Nothing in this module hits the network. Inputs are plain dicts / numbers so
the backtest engine can replay identical logic over historical candles.
"""

from __future__ import annotations

from typing import Optional

from supercrypto.config import DD_WEIGHTS, FNG_RISK_OFF, FNG_RISK_ON


# ── Due diligence ──────────────────────────────────────────────────────────


def holder_concentration_score(
    pair_count: int,
    top_pair_liq_share: float,
    mcap: float,
    vol: float,
) -> tuple[float, str]:
    """Free-data proxy for float concentration.

    Classic rug shape: a single DEX pair holding ~100% of observed liquidity.
    Real on-chain top-10 holder share (OnChainHolder agent) overrides this
    when an Etherscan key is present; this is the always-on fallback.
    """
    if pair_count <= 0:
        return 0.15, "no DEX pairs observed — concentration unknown"
    if pair_count == 1 and top_pair_liq_share >= 0.90:
        return 0.15, "single-pair concentration risk (top pair holds {:.0f}% of liq)".format(
            top_pair_liq_share * 100
        )
    if top_pair_liq_share >= 0.85:
        return 0.35, "liquidity concentrated ({:.0f}% in top pair)".format(
            top_pair_liq_share * 100
        )
    if pair_count >= 3 and top_pair_liq_share < 0.70:
        return 0.90, "liquidity spread across {} pairs (top {:.0f}%)".format(
            pair_count, top_pair_liq_share * 100
        )
    return 0.60, "moderate liquidity spread ({} pairs, top {:.0f}%)".format(
        pair_count, top_pair_liq_share * 100
    )


def market_traction_score(
    change_7d: Optional[float],
    change_30d: Optional[float],
    rank: Optional[int],
) -> tuple[float, str]:
    """7d/30d price change + rank. Fading names get penalized, not ignored."""
    c7 = change_7d if change_7d is not None else 0.0
    c30 = change_30d if change_30d is not None else 0.0

    if c7 <= -20 and c30 <= -30:
        return 0.15, "fading traction (7d {:+.0f}%, 30d {:+.0f}%)".format(c7, c30)
    if c7 <= -10 and c30 <= -15:
        return 0.30, "weak traction (7d {:+.0f}%, 30d {:+.0f}%)".format(c7, c30)

    score = 0.45
    notes = []
    if c7 > 10:
        score += 0.20
        notes.append("7d {:+.0f}%".format(c7))
    elif c7 > 0:
        score += 0.10
        notes.append("7d {:+.0f}%".format(c7))
    else:
        notes.append("7d {:+.0f}%".format(c7))

    if c30 > 20:
        score += 0.15
        notes.append("30d {:+.0f}%".format(c30))
    elif c30 < -20:
        score -= 0.10

    if rank is not None:
        if rank < 500:
            score += 0.10
            notes.append("rank #{}".format(rank))
        elif rank > 3000:
            score -= 0.10
            notes.append("rank #{} (thin)".format(rank))

    return max(0.0, min(1.0, score)), ", ".join(notes) or "no traction data"


def _liquidity_health(mcap: float, vol: float, rag: str) -> tuple[float, str]:
    if rag != "indexed":
        return 0.20, "not fully indexed — liquidity unverified"
    if mcap > 5_000_000:
        return 0.90, "mcap > $5M (${:.1f}M)".format(mcap / 1e6)
    if mcap > 500_000:
        return 0.55, "mcap ${:.1f}M — thin, be careful".format(mcap / 1e6)
    if mcap > 0:
        return 0.25, "microcap ${:.2f}M".format(mcap / 1e6)
    return 0.10, "no market-cap data"


def _volume_depth(mcap: float, vol: float, price: float) -> tuple[float, str]:
    if price <= 0:
        return 0.20, "no price — volume depth unknown"
    if mcap > 0 and vol / mcap > 0.05:
        return 0.85, "healthy volume/mcap {:.2f}".format(vol / mcap)
    if vol < 10_000:
        return 0.15, "very low volume — exit liquidity risk"
    if mcap > 0:
        return 0.45, "volume/mcap {:.3f}".format(vol / mcap)
    return 0.30, "volume ${:,.0f}".format(vol)


def _age_survivorship(rag: str, ath_change_pct: float, price: float) -> tuple[float, str]:
    if rag in ("indexed", "indexed_unverified"):
        score = 0.70
        note = "found on CoinGecko"
        if price > 0 and ath_change_pct > -40:
            score = 0.90
            note = "indexed; only {:.0f}% below ATH".format(ath_change_pct)
        return score, note
    if rag == "unknown_to_major_indexers":
        return 0.10, "unknown to major indexers — verify contract"
    return 0.30, "survivorship unverified"


def score_dd(info: dict) -> tuple[float, list[str]]:
    """Weighted sum of the five DD components. All five are always computed.

    ox-alpha defined holder_concentration and market_traction as weights but
    `_score()` never produced a value for either. That is the bug this closes.
    """
    mcap = info.get("mcap") or 0
    vol = info.get("vol") or 0
    price = info.get("price") or 0
    rag = info.get("rag_status") or "unknown"
    ath = info.get("ath_change_pct") or 0
    pair_count = int(info.get("pair_count") or 0)
    top_share = float(info.get("top_pair_liq_share") or (1.0 if pair_count <= 1 else 0.6))
    change_7d = info.get("change_7d")
    change_30d = info.get("change_30d")
    rank = info.get("rank")

    components = {
        "liquidity_health": _liquidity_health(mcap, vol, rag),
        "holder_concentration": holder_concentration_score(pair_count, top_share, mcap, vol),
        "volume_depth": _volume_depth(mcap, vol, price),
        "age_survivorship": _age_survivorship(rag, ath, price),
        "market_traction": market_traction_score(change_7d, change_30d, rank),
    }

    score = 0.0
    reasons = []
    for name, weight in DD_WEIGHTS.items():
        value, note = components[name]
        score += weight * value
        reasons.append("{}: {}".format(name, note))
    return min(1.0, max(0.0, score)), reasons


# ── Whale / momentum (used live AND in backtest) ───────────────────────────


def score_whale(change_pct: float, vol_ratio: float, vol: float) -> tuple[str, float, list[str]]:
    """Return (signal_name, confidence, reasons). signal_name is whale_up/down
    or empty if below the 0.5 emit threshold."""
    score = 0.0
    reasons = []
    direction = "up"

    if change_pct > 15:
        score += 0.25
        reasons.append("24h {:+.1f}%".format(change_pct))
    elif change_pct < -15:
        score += 0.20
        direction = "down"
        reasons.append("24h {:+.1f}%".format(change_pct))

    if vol_ratio > 3:
        score += 0.35
        reasons.append("volume {:.1f}x normal".format(vol_ratio))
    elif vol_ratio > 2:
        score += 0.20
        reasons.append("volume {:.1f}x normal".format(vol_ratio))

    if change_pct > 5 and vol_ratio > 2:
        score += 0.15
        reasons.append("momentum + volume confirmed")

    if vol >= 500_000:
        score += 0.10
        reasons.append("real liquidity")

    if score < 0.5:
        return "", 0.0, reasons
    name = "whale_up" if direction == "up" else "whale_down"
    return name, min(1.0, score), reasons


# ── Macro regime ───────────────────────────────────────────────────────────


def classify_regime(fear_greed: Optional[int], btc_dominance: Optional[float]) -> str:
    """risk_on / neutral / risk_off from Fear & Greed + BTC dominance."""
    fng = fear_greed if fear_greed is not None else 50
    dom = btc_dominance if btc_dominance is not None else 50.0
    if fng <= FNG_RISK_OFF or dom >= 60.0:
        return "risk_off"
    if fng >= FNG_RISK_ON and dom <= 55.0:
        return "risk_on"
    return "neutral"


def regime_multiplier(regime: str) -> float:
    return {"risk_on": 1.15, "neutral": 1.0, "risk_off": 0.70}.get(regime, 1.0)


# ── Advisor composite ──────────────────────────────────────────────────────


def score_verdict(
    bias: float,
    signal_count: int,
    dd_score: Optional[float],
    has_dd: bool,
    regime: str = "neutral",
    holder_red_flag: bool = False,
    buy_threshold: float = 0.15,
    dd_buy_threshold: float = 0.4,
    holder_cut: float = 0.30,
) -> tuple[str, float]:
    """Composite score → action. Risk manager may later downgrade, never invent.

    Returns (action, score). action in {BUY, HOLD, WATCH, AVOID}.
    """
    base = bias / max(signal_count, 1)
    if has_dd:
        score = base * (0.4 + 0.6 * (dd_score or 0.0))
    else:
        score = base * 0.35
        dd_score = 0.0

    score *= regime_multiplier(regime)

    if holder_red_flag:
        score *= (1.0 - holder_cut)

    if score >= buy_threshold and has_dd and (dd_score or 0) >= dd_buy_threshold:
        action = "BUY"
    elif score < 0:
        action = "AVOID"
    elif has_dd and (dd_score or 0) >= dd_buy_threshold and score >= 0.05:
        action = "HOLD"
    else:
        action = "WATCH"

    return action, round(score, 3)
