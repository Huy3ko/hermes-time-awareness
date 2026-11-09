"""Tests für hermes-time-awareness — reine Logik, keine Hermes-Installation nötig."""

import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "__init__.py"


def _load():
    spec = importlib.util.spec_from_file_location("hta", PKG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_hermes(prev_ts=None, db_calls=None):
    """Minimale Hermes-Oberfläche, damit das Modul ohne Gateway lädt."""
    const = types.ModuleType("hermes_constants")
    const.get_hermes_home = lambda: Path("/nonexistent-home")
    sys.modules["hermes_constants"] = const

    ht = types.ModuleType("hermes_time")
    ht.get_timezone = lambda: datetime.now().astimezone().tzinfo
    sys.modules["hermes_time"] = ht

    cli = types.ModuleType("hermes_cli")
    cfgmod = types.ModuleType("hermes_cli.config")
    cfgmod.load_config = lambda: {}
    cli.config = cfgmod
    sys.modules["hermes_cli"] = cli
    sys.modules["hermes_cli.config"] = cfgmod
    return const


# ── Tagesabschnitt: Grenzen exakt ─────────────────────────────────────────

def test_day_period_boundaries():
    m = _load()
    assert m._day_period(5) == "night"
    assert m._day_period(6) == "morning"
    assert m._day_period(11) == "morning"
    assert m._day_period(12) == "midday"
    assert m._day_period(13) == "midday"
    assert m._day_period(14) == "afternoon"
    assert m._day_period(17) == "afternoon"
    assert m._day_period(18) == "evening"
    assert m._day_period(21) == "evening"
    assert m._day_period(22) == "night"
    assert m._day_period(23) == "night"
    assert m._day_period(0) == "night"


# ── Grobe Formulierung ────────────────────────────────────────────────────

def test_humanize_tiers():
    m = _load()
    assert m._humanize(30) == "a minute"
    assert m._humanize(40 * 60) == "40 minutes"
    assert m._humanize(90 * 60) == "2 hours"        # 1,5 h rundet auf 2
    assert m._humanize(5 * 3600) == "5 hours"
    assert m._humanize(23.6 * 3600) == "a day"      # rundet nicht auf 24 h
    assert m._humanize(86400) == "a day"
    assert m._humanize(3 * 86400) == "3 days"


# ── Uhr-Block ─────────────────────────────────────────────────────────────

def test_clock_block_always_present():
    _stub_hermes()
    m = _load()
    out = m._build_context(None, 0, include_gap=False)
    assert out.startswith("[Temporal context]")
    assert "Now: " in out
    assert "Europe/Berlin" in out or "UTC" in out
    assert "Last exchange:" not in out
    assert "[/Temporal context]" in out
    assert "never quote" in out, "die Modell-Instruktion muss am Block hängen"


def test_gap_line_only_when_requested():
    _stub_hermes()
    m = _load()
    now = datetime.now().timestamp()
    out = m._build_context(now - 2 * 3600, now, include_gap=True)
    assert "Last exchange:" in out
    assert "2 hours ago" in out
    assert "today" in out


# ── Abfrage-Cache ─────────────────────────────────────────────────────────

def test_query_cache_avoids_second_db_hit():
    _stub_hermes()
    m = _load()
    calls = {"n": 0}
    m._last_exchange_ts = lambda sid: (calls.__setitem__("n", calls["n"] + 1), 1234.0)[1]
    first = m._cached_last_exchange("s1", 60.0)
    second = m._cached_last_exchange("s1", 60.0)
    assert first == 1234.0 and second == 1234.0
    assert calls["n"] == 1, "zweite Abfrage innerhalb der TTL darf die DB nicht erneut lesen"


def test_query_cache_expires():
    _stub_hermes()
    m = _load()
    calls = {"n": 0}
    m._last_exchange_ts = lambda sid: (calls.__setitem__("n", calls["n"] + 1), 99.0)[1]
    m._cached_last_exchange("s2", 0.0)   # TTL 0 → jede Abfrage neu
    m._cached_last_exchange("s2", 0.0)
    assert calls["n"] == 2


# ── Schwellen und Plattform ───────────────────────────────────────────────

def test_below_threshold_has_no_gap_line():
    _stub_hermes()
    m = _load()
    m._cfg = lambda: {"min_gap_seconds": 7200, "notify_date_change": True}
    m._cached_last_exchange = lambda sid, ttl: datetime.now().timestamp() - 60
    out = m._pre_llm_call(session_id="s3", platform="telegram")["context"]
    assert "Latest exchange" not in out and "Last exchange:" not in out
    assert "Now: " in out


def test_cron_platform_gets_clock_but_no_gap():
    _stub_hermes()
    m = _load()
    m._cfg = lambda: {"min_gap_seconds": 60, "notify_date_change": True}
    m._cached_last_exchange = lambda sid, ttl: datetime.now().timestamp() - 99999
    out = m._pre_llm_call(session_id="s4", platform="cron")["context"]
    assert "Now: " in out
    assert "Last exchange:" not in out


def test_disabled_returns_none():
    _stub_hermes()
    m = _load()
    m._cfg = lambda: {"enabled": False}
    assert m._pre_llm_call(session_id="s5", platform="telegram") is None


def test_fail_open_still_returns_clock():
    _stub_hermes()
    m = _load()
    def boom(*a, **k):
        raise RuntimeError("kaputt")
    m._cfg = boom
    out = m._pre_llm_call(session_id="s6", platform="telegram")
    assert out is not None and "Now: " in out["context"]


# ── Notification-Erkennung ────────────────────────────────────────────────

def test_notification_markers():
    _stub_hermes()
    m = _load()
    m._cfg = lambda: {"exclude_user_prefixes": ["[background-event "]}
    assert m._notification("hallo", "notification", None) is True
    assert m._notification("hallo", None, '{"notification_category":"reminder"}') is True
    assert m._notification("[background-event timer] los", None, None) is True
    assert m._notification("normale Nachricht", None, None) is False
