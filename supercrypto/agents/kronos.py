"""Kronos Forecast agent — foundation-model candlestick forecasts for crypto.

Wraps Kronos (https://github.com/shiyu-coder/Kronos, MIT, vendored under
vendor/kronos/) — a decoder-only Transformer pre-trained on K-line (OHLCV)
sequences from 45+ exchanges, crypto included — and turns its probabilistic
forecasts into ordinary bus signals that the advisor and WeightLearner
already know how to weigh.

Per coin:
  1. Pull the last `lookback` COMPLETE 4h candles (Binance public data API,
     KuCoin fallback). The still-open candle is dropped so Kronos never reads
     a half-formed bar as finished; its last price is kept as the live
     reference price instead.
  2. Crypto trades 24/7, so future timestamps are simply +4h steps.
  3. Sample `n_paths` independent forecast paths. Kronos' own predict()
     averages its samples, which throws the uncertainty away; here every
     path is its own batch row so agreement can be measured.
  4. Target = forecast close 24h out (6 x 4h bars) — the same horizon the
     learning loop settles (`outcome_24h`).
  5. Emit kronos_forecast_up / kronos_forecast_down only when the paths
     mostly agree on direction, the mean move is statistically distinct from
     zero across paths (t-stat), AND it clears a noise floor. Otherwise no
     signal. If torch / the weights can't be loaded the agent emits nothing —
     no fabricated evidence ever reaches the advisor.

The WeightLearner then tracks both signal types like any other, so if Kronos
has no edge on these coins its influence decays on its own.

Requires the optional deps in requirements-kronos.txt. Disable with
KRONOS_ENABLED=0.
"""

from __future__ import annotations

import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone

import json

from supercrypto.config import (
    KRONOS_FORECASTS_FILE,
    KRONOS_HISTORY_KEEP,
    KRONOS_INTERVAL_HOURS,
    KRONOS_LOOKBACK,
    KRONOS_MAX_COINS,
    KRONOS_MAX_MOVE_VS_VOL,
    KRONOS_MIN_AGREEMENT,
    KRONOS_MIN_HISTORY,
    KRONOS_MIN_MOVE_PCT,
    KRONOS_MIN_T_STAT,
    KRONOS_MODEL_ID,
    KRONOS_N_PATHS,
    KRONOS_PRED_LEN,
    KRONOS_TOKENIZER_ID,
    ROOT,
)
from supercrypto.core.base import SIGNALS_FILE, BaseAgent, api_get, fetch_markets

VENDOR_DIR = os.path.join(ROOT, "vendor", "kronos")
BINANCE_DATA = "https://data-api.binance.vision/api/v3/klines"  # not geo-blocked on CI
KUCOIN_CANDLES = "https://api.kucoin.com/api/v1/market/candles"
CORE_COINS = ["BTC", "ETH", "SOL", "BNB", "XRP"]
SKIP = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "BUSD", "PYUSD", "USDS"}
PAIR_ALIASES = {"MATIC": "POL"}  # Binance renamed the pair


# ── Pure decision logic (unit-tested without torch / numpy) ──────────────

def decide(path_rets, daily_vol_pct=None,
           min_move=KRONOS_MIN_MOVE_PCT, min_agree=KRONOS_MIN_AGREEMENT,
           min_t=KRONOS_MIN_T_STAT, max_z=KRONOS_MAX_MOVE_VS_VOL):
    """path_rets: % move to target per sampled path.

    Returns None (no signal) or dict(direction, mean, agreement, t_stat, conf).
    """
    rets = [float(r) for r in path_rets]
    if len(rets) < 2 or not all(math.isfinite(r) for r in rets):
        return None
    mean = statistics.fmean(rets)
    if mean == 0:
        return None
    direction = 1 if mean > 0 else -1
    agreement = sum(1 for r in rets if (r > 0) == (direction > 0) and r != 0) / len(rets)
    sd = statistics.stdev(rets)
    t_stat = mean / (sd / math.sqrt(len(rets))) if sd > 0 else math.copysign(math.inf, mean)
    if abs(mean) < min_move or agreement < min_agree or abs(t_stat) < min_t:
        return None
    z = mean / daily_vol_pct if daily_vol_pct else None
    if max_z and z is not None and abs(z) > max_z:
        return None  # implausibly large vs the coin's own volatility: don't trust it
    # 0.45 .. 0.85: agreement does most of the work; a big move relative to
    # the coin's own volatility adds a little. Deliberately modest — the
    # weight learner decides what Kronos is really worth.
    conf = 0.45 + 0.3 * (agreement - 0.5) * 2
    if z is not None:
        conf += 0.1 * min(abs(z), 1.5)
    return {
        "direction": direction,
        "mean": mean,
        "agreement": agreement,
        "t_stat": t_stat,
        "z": z,
        "conf": round(min(conf, 0.85), 3),
    }


def percentile(values, q):
    s = sorted(values)
    if not s:
        return None
    k = (len(s) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def grade(record, actual_close):
    """Fill a stored forecast with what really happened at its target time."""
    ref = record.get("reference_price")
    if not ref or not actual_close:
        return record
    actual = (actual_close - ref) / ref * 100.0
    exp = record.get("expected_move_pct") or 0.0
    lo, hi = (record.get("range_pct") or [None, None])[:2]
    record.update({
        "actual_close": actual_close,
        "actual_move_pct": round(actual, 2),
        "direction_hit": (actual > 0) == (exp > 0) and actual != 0,
        "in_range": lo is not None and hi is not None and lo <= actual <= hi,
        "abs_error_pct": round(abs(actual - exp), 2),
        "graded": True,
    })
    return record


def track_record(history):
    """Summary stats over graded forecasts (all, and those that became signals)."""
    def summarise(rows):
        n = len(rows)
        if not n:
            return {"n": 0, "hit_rate": None, "in_range": None, "mae": None, "avg_return": None}
        hits = sum(1 for r in rows if r.get("direction_hit"))
        inr = sum(1 for r in rows if r.get("in_range"))
        mae = sum(r.get("abs_error_pct", 0) for r in rows) / n
        # return you'd have made following the forecast direction
        ret = sum(r["actual_move_pct"] * (1 if r.get("expected_move_pct", 0) > 0 else -1) for r in rows) / n
        return {"n": n, "hit_rate": round(hits / n * 100, 1), "in_range": round(inr / n * 100, 1),
                "mae": round(mae, 2), "avg_return": round(ret, 2)}

    graded = [r for r in history if r.get("graded")]
    signaled = [r for r in graded if r.get("signal")]
    by_coin = {}
    for r in graded:
        by_coin.setdefault(r["coin"], []).append(r)
    return {
        "all": summarise(graded),
        "signals": summarise(signaled),
        "by_coin": {c: summarise(v) for c, v in by_coin.items()},
        "pending": sum(1 for r in history if not r.get("graded")),
    }


# ── Agent ────────────────────────────────────────────────────────────────

class KronosForecast(BaseAgent):
    NAME = "kronos"
    EMOJI = "🔮"

    def __init__(self, predictor=None, **kw):
        super().__init__(**kw)
        self._predictor = predictor              # injectable for tests
        self._load_attempted = predictor is not None
        self.available = predictor is not None
        self.unavailable_reason = None
        self.last_run_stats = {}

    def default_weights(self):
        return {"kronos_forecast_up": 0.35, "kronos_forecast_down": 0.35}

    # ── model loading (lazy; any failure = agent sits out) ──
    def _load(self) -> bool:
        if self._load_attempted:
            return self.available
        self._load_attempted = True
        if os.environ.get("KRONOS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            self.unavailable_reason = "disabled via KRONOS_ENABLED"
            return False
        try:
            import torch
            if VENDOR_DIR not in sys.path:
                sys.path.insert(0, VENDOR_DIR)
            from model import Kronos, KronosPredictor, KronosTokenizer

            torch.set_num_threads(max(1, os.cpu_count() or 1))
            tokenizer = KronosTokenizer.from_pretrained(KRONOS_TOKENIZER_ID)
            model = Kronos.from_pretrained(KRONOS_MODEL_ID)
            tokenizer.eval()
            model.eval()
            self._predictor = KronosPredictor(model, tokenizer, max_context=512)
            self.available = True
        except Exception as e:  # ImportError (no torch), HF download failure, ...
            self.unavailable_reason = "%s: %s" % (type(e).__name__, str(e)[:160])
            self.available = False
        return self.available

    # ── candles ──
    @staticmethod
    def _binance(sym: str, limit: int):
        pair = PAIR_ALIASES.get(sym, sym) + "USDT"
        data = api_get(BINANCE_DATA, params={
            "symbol": pair, "interval": "%dh" % KRONOS_INTERVAL_HOURS, "limit": limit})
        if not isinstance(data, list) or not data:
            return None
        # [open_time, o, h, l, c, vol, close_time, quote_vol, ...]
        return [(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                 float(k[5]), float(k[7])) for k in data]

    @staticmethod
    def _kucoin(sym: str, limit: int):
        end = int(time.time())
        start = end - limit * KRONOS_INTERVAL_HOURS * 3600
        data = api_get(KUCOIN_CANDLES, params={
            "type": "%dhour" % KRONOS_INTERVAL_HOURS, "symbol": "%s-USDT" % sym,
            "startAt": start, "endAt": end})
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        if not rows:
            return None
        # [time, open, close, high, low, volume, turnover], newest first
        out = [(int(r[0]), float(r[1]), float(r[3]), float(r[4]), float(r[2]),
                float(r[5]), float(r[6])) for r in rows]
        return sorted(out)

    def _candles(self, sym: str):
        """Returns (complete_bars, live_price) or (None, None)."""
        limit = KRONOS_LOOKBACK + 2
        bars = self._binance(sym, limit) or self._kucoin(sym, limit)
        if not bars:
            return None, None
        step = KRONOS_INTERVAL_HOURS * 3600
        live = bars[-1][4]
        now = time.time()
        complete = [b for b in bars if b[0] + step <= now and b[4] > 0 and min(b[1:5]) > 0]
        return complete[-KRONOS_LOOKBACK:], live

    # ── candidates ──
    def _candidates(self, coins=None):
        if coins:
            return [c.upper() for c in coins if c.upper() not in SKIP][:KRONOS_MAX_COINS]
        counts = {}
        for s in self.read_signals():
            c = (s.get("coin") or "").upper()
            if c and c not in SKIP:
                counts[c] = counts.get(c, 0) + 1
        ranked = [c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])]
        out = []
        for c in CORE_COINS + ranked:
            if c not in out:
                out.append(c)
        return out[:KRONOS_MAX_COINS]

    # ── inference ──
    def _forecast(self, prepared: dict) -> dict:
        """prepared: {sym: item} -> {sym: [[close per future bar] per path]}."""
        groups = {}
        for sym, item in prepared.items():
            groups.setdefault(len(item["bars"]), []).append(sym)

        out = {}
        for _, syms in groups.items():  # predict_batch needs equal context lengths
            rows = [(s, p) for s in syms for p in range(KRONOS_N_PATHS)]
            for start in range(0, len(rows), 64):
                chunk = rows[start:start + 64]
                try:
                    preds = self._predictor.predict_batch(
                        df_list=[prepared[s]["df"] for s, _ in chunk],
                        x_timestamp_list=[prepared[s]["x_ts"] for s, _ in chunk],
                        y_timestamp_list=[prepared[s]["y_ts"] for s, _ in chunk],
                        pred_len=KRONOS_PRED_LEN, T=1.0, top_p=0.9,
                        sample_count=1, verbose=False,
                    )
                except Exception as e:
                    print("  🔮 kronos — batch failed (%s: %s)" % (type(e).__name__, e))
                    continue
                for (s, _), pdf in zip(chunk, preds):
                    out.setdefault(s, []).append([float(v) for v in pdf["close"].tolist()])
        return {s: v for s, v in out.items() if len(v) == KRONOS_N_PATHS}

    def _prepare(self, sym, bars, live):
        import pandas as pd

        step = KRONOS_INTERVAL_HOURS * 3600
        ts = pd.to_datetime([b[0] for b in bars], unit="s")
        df = pd.DataFrame({
            "open": [b[1] for b in bars], "high": [b[2] for b in bars],
            "low": [b[3] for b in bars], "close": [b[4] for b in bars],
            "volume": [b[5] for b in bars], "amount": [b[6] for b in bars],
        })
        future = pd.to_datetime([bars[-1][0] + step * (i + 1) for i in range(KRONOS_PRED_LEN)], unit="s")
        closes = [b[4] for b in bars]
        per_day = max(1, 24 // KRONOS_INTERVAL_HOURS)
        daily = closes[::-1][::per_day][::-1][-31:]
        rets = [math.log(b / a) for a, b in zip(daily, daily[1:]) if a > 0 and b > 0]
        vol = statistics.stdev(rets) * 100 if len(rets) > 5 else None
        return {
            "bars": bars, "df": df, "x_ts": pd.Series(ts), "y_ts": pd.Series(future),
            "future": [t.isoformat() + "Z" for t in future],
            "ref": float(live or closes[-1]), "daily_vol_pct": vol,
        }

    def _scan_seed(self):
        # Same 6h scan slot -> same sampled paths, so a re-run doesn't flip a
        # call on sampling noise alone.
        slot = int(time.time() // (6 * 3600))
        try:
            import torch
            torch.manual_seed(slot)
        except Exception:
            pass
        try:
            import numpy as np
            np.random.seed(slot % (2 ** 32))
        except Exception:
            pass

    # ── forecast store / grading ──
    @staticmethod
    def load_store():
        try:
            with open(KRONOS_FORECASTS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {"latest": {}, "history": [], "stats": {}, "track_record": {}}

    def _actual_close(self, sym, target_iso):
        """Close of the 4h candle that ends at target time (Binance, KuCoin fallback)."""
        try:
            target = datetime.fromisoformat(target_iso.replace("Z", "+00:00"))
        except ValueError:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        open_s = int(target.timestamp()) - KRONOS_INTERVAL_HOURS * 3600
        pair = PAIR_ALIASES.get(sym, sym) + "USDT"
        data = api_get(BINANCE_DATA, params={
            "symbol": pair, "interval": "%dh" % KRONOS_INTERVAL_HOURS,
            "startTime": open_s * 1000, "limit": 1})
        if isinstance(data, list) and data and int(data[0][0]) // 1000 == open_s:
            return float(data[0][4])
        data = api_get(KUCOIN_CANDLES, params={
            "type": "%dhour" % KRONOS_INTERVAL_HOURS, "symbol": "%s-USDT" % sym,
            "startAt": open_s, "endAt": open_s + KRONOS_INTERVAL_HOURS * 3600})
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        for r in rows or []:
            if int(r[0]) == open_s:
                return float(r[2])
        return None

    def _grade_due(self, store):
        now = datetime.now(timezone.utc)
        graded = 0
        for rec in store.get("history", []):
            if rec.get("graded"):
                continue
            try:
                target = datetime.fromisoformat(rec["target_time"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            if target > now:
                continue
            close = self._actual_close(rec["coin"], rec["target_time"])
            if close:
                grade(rec, close)
                graded += 1
            elif (now - target).days > 3:
                rec.update({"graded": False, "expired": True})
        store["history"] = [r for r in store.get("history", []) if not r.get("expired")][-KRONOS_HISTORY_KEEP:]
        return graded

    def _replace_own_bus_signals(self, max_age_h=None):
        # The bus persists between scans; drop earlier Kronos calls so a stale
        # forecast never lingers next to (or contradicts) a fresh one. When the
        # model can't run this scan, only calls past their 24h horizon go.
        bus = self.read_signals()

        def stale(b):
            if b.get("agent") != self.name:
                return False
            if max_age_h is None:
                return True
            try:
                ts = datetime.fromisoformat(str(b.get("timestamp")).replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - ts).total_seconds() > max_age_h * 3600
            except ValueError:
                return True

        kept = [b for b in bus if not stale(b)]
        if len(kept) != len(bus):
            with open(SIGNALS_FILE, "w") as f:
                json.dump(kept, f, indent=2)

    # ── run ──
    def run(self, **kwargs):
        stats = {"requested": 0, "eligible": 0, "forecast": 0, "signals": 0,
                 "skipped": {}, "seconds": 0.0, "model": KRONOS_MODEL_ID}
        self.last_run_stats = stats
        self._replace_own_bus_signals(max_age_h=30)
        store = self.load_store()
        stats["graded_now"] = self._grade_due(store)
        if not self._load():
            stats["unavailable"] = self.unavailable_reason
            print("  🔮 kronos — sitting out:", self.unavailable_reason)
            self._save(store, stats)
            return []

        self._replace_own_bus_signals()
        cands = self._candidates(kwargs.get("coins"))
        stats["requested"] = len(cands)
        prepared = {}
        for sym in cands:
            bars, live = self._candles(sym)
            if not bars:
                stats["skipped"]["no_candles"] = stats["skipped"].get("no_candles", 0) + 1
                continue
            if len(bars) < KRONOS_MIN_HISTORY:
                stats["skipped"]["short_history"] = stats["skipped"].get("short_history", 0) + 1
                continue
            prepared[sym] = self._prepare(sym, bars, live)
            time.sleep(0.1)
        stats["eligible"] = len(prepared)
        if not prepared:
            self._save(store, stats)
            return []

        t0 = time.time()
        self._scan_seed()
        paths = self._forecast(prepared)
        stats["seconds"] = round(time.time() - t0, 1)

        cg_prices = {c.get("symbol", "").upper(): c.get("current_price") for c in fetch_markets()}
        signals, latest = [], {}
        made_at = datetime.now(timezone.utc).isoformat()
        for sym, item in prepared.items():
            p = paths.get(sym)
            if not p:
                continue
            stats["forecast"] += 1
            fc = self._summarise(item, p)
            sig = self._to_signal(sym, fc, cg_prices.get(sym))
            fc["signal"] = sig["signal"] if sig else None
            fc["confidence"] = sig["confidence"] if sig else None
            fc["made_at"] = made_at
            latest[sym] = fc
            store.setdefault("history", []).append({
                "coin": sym, "made_at": made_at, "target_time": fc["target_time"],
                "reference_price": fc["reference_price"],
                "expected_move_pct": fc["expected_move_pct"], "range_pct": fc["range_pct"],
                "path_agreement": fc["path_agreement"], "signal": fc["signal"],
            })
            if sig:
                signals.append(sig)
        stats["signals"] = len(signals)
        store["latest"] = latest
        self._save(store, stats)
        print("  🔮 kronos — %s" % {k: v for k, v in stats.items() if k != "model"})
        return signals

    def _summarise(self, item, paths):
        """Everything the dashboard needs about one coin's forecast."""
        ref = item["ref"]
        t = KRONOS_PRED_LEN - 1  # 24h out
        rets = [(path[t] - ref) / ref * 100.0 for path in paths]
        d = decide(rets, item["daily_vol_pct"], min_move=0, min_agree=0, min_t=0, max_z=0) or {}
        cols = list(zip(*paths))
        bars = item["bars"]
        step = KRONOS_INTERVAL_HOURS * 3600
        hist = bars[-42:]  # last 7 days of 4h closes for the chart
        mean = statistics.fmean(rets)
        agree = d.get("agreement", 0.0)
        t_stat = d.get("t_stat")
        return {
            "model": KRONOS_MODEL_ID,
            "horizon_h": KRONOS_PRED_LEN * KRONOS_INTERVAL_HOURS,
            "target_time": item["future"][t],
            "expected_move_pct": round(mean, 2),
            "range_pct": [round(percentile(rets, 10), 2), round(percentile(rets, 90), 2)],
            "path_agreement": round(agree, 2),
            "t_stat": round(t_stat, 2) if t_stat is not None and math.isfinite(t_stat) else None,
            "paths": len(rets),
            "daily_vol_pct": round(item["daily_vol_pct"], 2) if item["daily_vol_pct"] else None,
            "move_vs_vol": round(mean / item["daily_vol_pct"], 2) if item["daily_vol_pct"] else None,
            "reference_price": ref,
            "_rets": rets,
            "history": [{"t": datetime.fromtimestamp(b[0] + step, timezone.utc).isoformat(),
                         "c": b[4]} for b in hist],
            "forecast": [{"t": ts, "median": statistics.median(col),
                          "p10": percentile(col, 10), "p90": percentile(col, 90)}
                         for ts, col in zip(item["future"], cols)],
            "context_bars": len(bars),
        }

    def _to_signal(self, sym, fc, cg_price=None):
        rets = fc.pop("_rets")
        d = decide(rets, fc["daily_vol_pct"])
        if not d:
            return None
        lo, hi = fc["range_pct"]
        name = "kronos_forecast_up" if d["direction"] > 0 else "kronos_forecast_down"
        reason = "Kronos 24h forecast %+.1f%% (%d/%d paths agree, 10-90%% range %+.1f..%+.1f%%)" % (
            d["mean"], round(d["agreement"] * len(rets)), len(rets), lo, hi)
        return {
            "coin": sym,
            "signal": name,
            "confidence": d["conf"],
            "source": "kronos",
            "details": {
                "reasons": [reason],
                # CoinGecko price so outcome settling compares like with like
                "price": cg_price or fc["reference_price"],
                "expected_move_pct": fc["expected_move_pct"],
                "range_pct": fc["range_pct"],
                "path_agreement": fc["path_agreement"],
                "target_time": fc["target_time"],
            },
        }

    def _save(self, store, stats):
        stats["at"] = datetime.now(timezone.utc).isoformat()
        for fc in store.get("latest", {}).values():
            fc.pop("_rets", None)
        store["stats"] = stats
        store["track_record"] = track_record(store.get("history", []))
        with open(KRONOS_FORECASTS_FILE, "w") as f:
            json.dump(store, f, indent=1)
        self.memory.setdefault("notes", {})["last_run"] = stats
        self.save_memory()
