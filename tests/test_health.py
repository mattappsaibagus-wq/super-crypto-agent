"""Offline tests for the health checks and the GitHub alert formatting."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import supercrypto.core.health as H  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def ago(h):
    return (NOW - timedelta(hours=h)).isoformat()


def test_bus_freshness_flags_expired_signals():
    bus = [{"signal": "microcap_opportunity", "timestamp": ago(30)},
           {"signal": "dd_result", "timestamp": ago(48)}]
    assert H.check_bus_freshness(bus, NOW)["status"] == "fail"
    assert H.check_bus_freshness(bus[1:], NOW)["status"] == "ok"   # DD lives 72h


def test_agents_active_warns_on_silent_core_agent():
    bus = [{"agent": "microcap", "timestamp": ago(1)}, {"agent": "dd", "timestamp": ago(1)}]
    c = H.check_agents_active(bus, NOW)
    assert c["status"] == "warn" and "macro" in c["detail"]


def test_paper_execution_fails_when_buys_not_opened():
    verdicts = [{"coin": "SAND", "action": "BUY", "suggested_size_pct": 4.3},
                {"coin": "BTC", "action": "HOLD", "suggested_size_pct": 0}]
    assert H.check_paper_execution(verdicts, {"positions": {}})["status"] == "fail"
    assert H.check_paper_execution(verdicts, {"positions": {"SAND": {}}})["status"] == "ok"


def test_price_coverage():
    assert H.check_price_coverage(["A", "B", "C", "D", "E"], {"A": 1}, [], [])["status"] == "fail"
    assert H.check_price_coverage(["A", "B"], {"A": 1, "B": 2}, [], [])["status"] == "ok"
    c = H.check_price_coverage(["A", "B", "C", "D", "E"], {c: 1 for c in "ABCD"}, ["E"], ["E"])
    assert c["status"] == "warn" and "voided: E" in c["detail"]


def test_kronos_checks():
    assert H.check_kronos({"stats": {"unavailable": "ImportError"}})["status"] == "fail"
    assert H.check_kronos({"stats": {"requested": 30, "forecast": 0}})["status"] == "fail"
    assert H.check_kronos({"stats": {"requested": 30, "forecast": 27, "signals": 6}})["status"] == "ok"
    weak = {"stats": {"requested": 30, "forecast": 27}, "track_record": {"all": {"n": 60, "hit_rate": 40.0}}}
    assert H.check_kronos(weak)["status"] == "warn"


def test_verdicts_and_drawdown():
    assert H.check_verdicts([], 40)["status"] == "fail"
    assert H.check_verdicts([{}] * 5, 40)["status"] == "warn"
    assert H.check_verdicts([{}] * 38, 40)["status"] == "ok"
    assert H.check_drawdown({"peak_equity": 100, "equity": 80})["status"] == "warn"


def test_run_checks_overall_is_worst_and_survives_bad_files():
    orig = (H.SIGNALS_FILE, H.VERDICTS_FILE, H.PAPER_FILE, H.KRONOS_FORECASTS_FILE, H.HEALTH_FILE)
    try:
        H.SIGNALS_FILE = H.VERDICTS_FILE = H.PAPER_FILE = H.KRONOS_FORECASTS_FILE = H.HEALTH_FILE = "/nonexistent"
        r = H.run_checks({}, now=NOW)
        assert r["overall"] == "fail"           # 0 verdicts
        assert len(r["history"]) == 1
    finally:
        H.SIGNALS_FILE, H.VERDICTS_FILE, H.PAPER_FILE, H.KRONOS_FORECASTS_FILE, H.HEALTH_FILE = orig


def test_alert_body_marks_failing_checks():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import health_alert
    fails = [{"id": "paper_execution", "name": "BUY verdicts executed", "detail": "no price | SAND"}]
    body = health_alert.body_for(fails, {"generated_at": "t", "code_version": "abc1234"})
    assert "<!-- fails: paper_execution -->" in body and "no price / SAND" in body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL", fn.__name__, "-", repr(e))
    print("%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
