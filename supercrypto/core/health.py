"""System health checks — run at the end of every scan.

Each check looks for the *symptom* of a class of failure rather than a
specific bug, e.g. "BUY verdicts that never became paper positions" catches
any broken price source, not just the CoinGecko rate-limit seen on 2026-10-05.

Results go to data/health.json (dashboard 🩺 panel). scripts/health_alert.py
turns a failing report into a GitHub Issue.

Statuses: ok < warn < fail. Every check is defensive: a check that crashes
reports itself as a warning instead of breaking the scan.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

from supercrypto.config import (
    DATA_DIR,
    KRONOS_FORECASTS_FILE,
    PAPER_FILE,
    SIGNAL_MAX_AGE_HOURS,
    SIGNAL_TTL_OVERRIDES,
    SIGNALS_FILE,
    VERDICTS_FILE,
)

HEALTH_FILE = os.path.join(DATA_DIR, "health.json")
RANK = {"ok": 0, "warn": 1, "fail": 2}

# Agents that run on every cloud scan and normally emit something.
CORE_AGENTS = ("microcap", "dd", "macro")
MIN_PRICE_COVERAGE = 0.8
KRONOS_MIN_GRADED = 30          # before judging the track record
KRONOS_MIN_HIT_RATE = 45.0      # % direction hits; below this Kronos is noise
VERDICT_DROP_RATIO = 0.3        # verdicts fall below 30% of last scan -> warn
DRAWDOWN_WARN_PCT = 15.0


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _age_h(ts, now):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 3600.0


def _check(cid, name, status, detail, **extra):
    return {"id": cid, "name": name, "status": status, "detail": detail, **extra}


# ── individual checks (pure: take loaded data, return a check dict) ───────

def check_bus_freshness(bus, now):
    stale, oldest = [], 0.0
    for s in bus:
        age = _age_h(s.get("timestamp"), now)
        if age is None:
            stale.append(s)
            continue
        oldest = max(oldest, age)
        if age > SIGNAL_TTL_OVERRIDES.get(s.get("signal"), SIGNAL_MAX_AGE_HOURS) + 1:
            stale.append(s)
    if stale:
        return _check("bus_freshness", "Signal freshness", "fail",
                      "%d signal(s) older than their expiry are still on the bus (oldest %.0fh) — "
                      "pruning isn't running." % (len(stale), oldest), oldest_h=round(oldest, 1))
    return _check("bus_freshness", "Signal freshness", "ok",
                  "%d signals, oldest %.0fh" % (len(bus), oldest), oldest_h=round(oldest, 1))


def check_agents_active(bus, now):
    fresh = {}
    for s in bus:
        age = _age_h(s.get("timestamp"), now)
        if age is not None and age <= 7:
            fresh[s.get("agent")] = fresh.get(s.get("agent"), 0) + 1
    silent = [a for a in CORE_AGENTS if not fresh.get(a)]
    status = "warn" if silent else "ok"
    detail = ("silent this scan: " + ", ".join(silent)) if silent else \
        "active: " + ", ".join("%s %d" % kv for kv in sorted(fresh.items()))
    return _check("agents_active", "Agents producing signals", status, detail, counts=fresh)


def check_paper_execution(verdicts, paper):
    held = set((paper.get("positions") or {}).keys())
    missed = [v["coin"] for v in verdicts
              if v.get("action") == "BUY" and (v.get("suggested_size_pct") or 0) > 0
              and v.get("coin") not in held]
    if missed:
        return _check("paper_execution", "BUY verdicts executed", "fail",
                      "BUY with a position size but no paper position: %s — usually no price "
                      "was found for it." % ", ".join(missed), missed=missed)
    n = sum(1 for v in verdicts if v.get("action") == "BUY")
    return _check("paper_execution", "BUY verdicts executed", "ok",
                  "%d BUY executed, %d positions open" % (n, len(held)))


def check_price_coverage(wanted, priced, mismatches, voided):
    wanted = list(wanted or [])
    if not wanted:
        return _check("price_coverage", "Prices found", "ok", "no prices needed this scan")
    got = [c for c in wanted if c in (priced or {})]
    cov = len(got) / len(wanted)
    missing = [c for c in wanted if c not in (priced or {})]
    note = ""
    if mismatches:
        note = " · same-ticker different token rejected: " + ", ".join(sorted(mismatches))
    if voided:
        note += " · voided: " + ", ".join(voided)
    status = "fail" if cov < MIN_PRICE_COVERAGE else ("warn" if missing else "ok")
    return _check("price_coverage", "Prices found", status,
                  "%d/%d priced%s%s" % (len(got), len(wanted),
                                        (" · missing: " + ", ".join(missing)) if missing else "", note),
                  coverage=round(cov, 2))


def check_kronos(store):
    stats = (store or {}).get("stats") or {}
    if not stats:
        return _check("kronos", "Kronos forecasts", "warn", "no Kronos run recorded yet")
    if stats.get("unavailable"):
        return _check("kronos", "Kronos forecasts", "fail",
                      "Kronos sat out: %s" % stats["unavailable"])
    req, fc = stats.get("requested") or 0, stats.get("forecast") or 0
    if req and fc == 0:
        return _check("kronos", "Kronos forecasts", "fail",
                      "0 of %d coins forecast (skipped: %s)" % (req, stats.get("skipped")))
    tr = ((store or {}).get("track_record") or {}).get("all") or {}
    detail = "%d/%d coins forecast, %d signals" % (fc, req, stats.get("signals") or 0)
    status = "warn" if req and fc < req * 0.5 else "ok"
    if (tr.get("n") or 0) >= KRONOS_MIN_GRADED and tr.get("hit_rate") is not None:
        detail += " · direction hit rate %s%% over %d graded" % (tr["hit_rate"], tr["n"])
        if tr["hit_rate"] < KRONOS_MIN_HIT_RATE:
            status = "warn"
            detail += " (below %d%% — no edge so far)" % KRONOS_MIN_HIT_RATE
    return _check("kronos", "Kronos forecasts", status, detail)


def check_verdicts(verdicts, previous_count):
    n = len(verdicts)
    if n == 0:
        return _check("verdicts", "Verdicts produced", "fail", "the advisor produced 0 verdicts")
    if previous_count and n < previous_count * VERDICT_DROP_RATIO:
        return _check("verdicts", "Verdicts produced", "warn",
                      "%d verdicts, down from %d last scan" % (n, previous_count))
    return _check("verdicts", "Verdicts produced", "ok", "%d verdicts" % n)


def check_drawdown(paper):
    start = paper.get("starting_equity") or 100.0
    peak = paper.get("peak_equity") or start
    eq = paper.get("equity") or start
    dd = (peak - eq) / peak * 100 if peak else 0.0
    status = "warn" if dd >= DRAWDOWN_WARN_PCT else "ok"
    return _check("drawdown", "Paper drawdown", status,
                  "equity %.2f, %.1f%% below peak" % (eq, dd), drawdown_pct=round(dd, 2))


# ── runner ───────────────────────────────────────────────────────────────

def _git_sha():
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:7]
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return None


def run_checks(ctx=None, now=None):
    """ctx (from the pipeline): price_wanted, prices, mismatches, voided."""
    ctx = ctx or {}
    now = now or datetime.now(timezone.utc)
    bus = _load(SIGNALS_FILE, [])
    verdicts = (_load(VERDICTS_FILE, {}) or {}).get("verdicts") or []
    paper = _load(PAPER_FILE, {}) or {}
    kronos = _load(KRONOS_FORECASTS_FILE, {}) or {}
    prev = _load(HEALTH_FILE, {}) or {}

    jobs = [
        lambda: check_bus_freshness(bus, now),
        lambda: check_agents_active(bus, now),
        lambda: check_verdicts(verdicts, prev.get("verdict_count")),
        lambda: check_paper_execution(verdicts, paper),
        lambda: check_price_coverage(ctx.get("price_wanted"), ctx.get("prices"),
                                     ctx.get("mismatches"), ctx.get("voided")),
        lambda: check_kronos(kronos),
        lambda: check_drawdown(paper),
    ]
    checks = []
    for job in jobs:
        try:
            checks.append(job())
        except Exception as e:  # a broken check must never break the scan
            checks.append(_check("check_error", "Health check error", "warn",
                                 "%s: %s" % (type(e).__name__, e)))
    overall = max((c["status"] for c in checks), key=RANK.get, default="ok")
    history = (prev.get("history") or [])[-59:] + [{"t": now.isoformat(), "overall": overall}]
    return {
        "generated_at": now.isoformat(),
        "overall": overall,
        "code_version": _git_sha(),
        "verdict_count": len(verdicts),
        "checks": checks,
        "history": history,
    }


def run_and_write(ctx=None):
    report = run_checks(ctx)
    with open(HEALTH_FILE, "w") as f:
        json.dump(report, f, indent=1)
    bad = [c for c in report["checks"] if c["status"] != "ok"]
    lines = "".join("\n    %s %s: %s" % ("✗" if c["status"] == "fail" else "!", c["name"], c["detail"])
                    for c in bad)
    print("[health] %s%s" % (report["overall"].upper(), lines))
    return report
