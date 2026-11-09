"""Portierte Testsuite aus time-gap v2.1.0, gegen hermes-time-awareness.

Alle 25 Tests der Vorlage, mit zwei bewussten Anpassungen:

1. `_plugin_config` heißt hier `_cfg`.
2. Das Plugin gibt IMMER einen Kontext zurück (die Uhr). Die Frage "feuert die
   Lücke?" wird deshalb über das Vorhandensein der Zeile `Last exchange:`
   geprüft, nicht über `is None`. Das ist die eigentliche Testabsicht der
   Vorlage: feuert die Lücke oder nicht.

Die DB-, WAL-, Session- und Notification-Tests sind unverändert übernommen —
sie prüfen Verhalten, nicht Format.
"""

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_DAY_S = 24 * 3600


def _make_state_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE messages (session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL)"
    )
    conn.commit()
    conn.close()


def _add_msg(path: Path, session_id: str, role: str, ts: float, content="x") -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, ?, ?, ?)",
        (session_id, role, content, ts),
    )
    conn.commit()
    conn.close()


def _gap_fired(result) -> bool:
    """Die Testabsicht der Vorlage: hat die Lücke den Turn erreicht?"""
    return bool(result) and "Last exchange:" in result.get("context", "")


@pytest.fixture
def tg(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "hta_under_test", _PLUGIN_DIR / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = tmp_path / "state.db"
    _make_state_db(db)
    mod._db = db
    monkeypatch.setattr(mod, "_db_path", lambda: db)
    monkeypatch.setattr(mod, "_cfg", lambda: {})
    # Eigene Session-Tabelle pro Test, nie die produktive.
    monkeypatch.setattr(mod, "_sessions_db_path", lambda: tmp_path / "time-awareness.db")
    mod._query_cache.clear()
    return mod


# ── Grundverhalten ────────────────────────────────────────────────────────

def test_no_prior_assistant_no_injection(tg):
    _add_msg(tg._db, "s1", "user", time.time())
    assert not _gap_fired(tg._pre_llm_call(session_id="s1"))


def test_missing_db_no_injection(tg, monkeypatch):
    monkeypatch.setattr(tg, "_db_path", lambda: tg._db.parent / "nope.db")
    assert not _gap_fired(tg._pre_llm_call(session_id="s1"))


def test_recent_exchange_no_injection(tg):
    _add_msg(tg._db, "s2", "assistant", time.time() - 5 * 60)
    assert not _gap_fired(tg._pre_llm_call(session_id="s2"))


def test_resume_after_gap_injects(tg):
    _add_msg(tg._db, "s3", "assistant", time.time() - 2 * _DAY_S)
    result = tg._pre_llm_call(session_id="s3")
    assert _gap_fired(result)
    assert "days" in result["context"]


def test_uses_last_assistant_not_user(tg):
    """Eine neue User-Zeile setzt die Uhr nicht zurück."""
    now = time.time()
    _add_msg(tg._db, "s4", "assistant", now - 2 * _DAY_S)
    _add_msg(tg._db, "s4", "user", now)
    result = tg._pre_llm_call(session_id="s4")
    assert _gap_fired(result)
    assert "days" in result["context"]


def test_picks_most_recent_assistant(tg):
    now = time.time()
    _add_msg(tg._db, "s5", "assistant", now - 5 * _DAY_S)
    _add_msg(tg._db, "s5", "assistant", now - 10 * 60)
    assert not _gap_fired(tg._pre_llm_call(session_id="s5"))


# ── Session-Isolation (echte DB, zwei Sessions) ───────────────────────────

def test_session_isolation(tg):
    now = time.time()
    _add_msg(tg._db, "old", "assistant", now - 3 * _DAY_S)
    _add_msg(tg._db, "fresh", "assistant", now - 60)
    assert not _gap_fired(tg._pre_llm_call(session_id="fresh"))
    assert _gap_fired(tg._pre_llm_call(session_id="old"))


# ── Formulierung ──────────────────────────────────────────────────────────

def test_graded_phrasing(tg):
    assert "minutes" in tg._humanize(40 * 60)
    assert "hours" in tg._humanize(5 * 3600)
    assert tg._humanize(25 * 3600) == "a day"
    assert tg._humanize(int(1.9 * 24 * 3600)) == "2 days"
    assert tg._humanize(3 * 24 * 3600) == "3 days"


# ── Konfiguration ─────────────────────────────────────────────────────────

def test_platform_exclusion(tg, monkeypatch):
    monkeypatch.setattr(tg, "_cfg", lambda: {"exclude_platforms": ["cron"]})
    _add_msg(tg._db, "s6", "assistant", time.time() - 5 * _DAY_S)
    assert not _gap_fired(tg._pre_llm_call(session_id="s6", platform="cron"))


def test_disabled_config(tg, monkeypatch):
    monkeypatch.setattr(tg, "_cfg", lambda: {"enabled": False})
    _add_msg(tg._db, "s7", "assistant", time.time() - 5 * _DAY_S)
    assert tg._pre_llm_call(session_id="s7") is None


def test_custom_threshold(tg, monkeypatch):
    monkeypatch.setattr(tg, "_cfg", lambda: {"min_gap_seconds": 10})
    _add_msg(tg._db, "s8", "assistant", time.time() - 20)
    assert _gap_fired(tg._pre_llm_call(session_id="s8"))


def test_empty_session_id(tg):
    assert not _gap_fired(tg._pre_llm_call(session_id=""))


# ── WAL-Sichtbarkeit: der wichtigste Test der Vorlage ─────────────────────

def test_reads_uncheckpointed_wal_commit(tg):
    """Committed-but-uncheckpointed WAL rows must be visible over mode=ro.

    Ohne diesen Test fällt ein Wechsel der Verbindungsart erst live auf —
    dann fehlt der Hinweis genau beim Wiedereinstieg nach einem Turn.
    """
    w = sqlite3.connect(str(tg._db))
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, ?, ?, ?)",
        ("wal", "assistant", "x", time.time() - 3 * _DAY_S),
    )
    w.commit()
    try:
        result = tg._pre_llm_call(session_id="wal")
        assert _gap_fired(result)
        assert "days" in result["context"]
    finally:
        w.close()


# ── Logging ───────────────────────────────────────────────────────────────

def test_log_silent_by_default(tg, monkeypatch, tmp_path):
    logfile = tmp_path / "debug.log"
    monkeypatch.setattr(tg, "_LOG_PATH", logfile)
    monkeypatch.setattr(tg, "_cfg", lambda: {})
    _add_msg(tg._db, "s-quiet", "assistant", time.time() - 3 * _DAY_S)
    tg._pre_llm_call(session_id="s-quiet")
    assert not logfile.exists()


def test_log_writes_when_debug_enabled(tg, monkeypatch, tmp_path):
    logfile = tmp_path / "debug.log"
    monkeypatch.setattr(tg, "_LOG_PATH", logfile)
    monkeypatch.setattr(tg, "_cfg", lambda: {"debug": True})
    _add_msg(tg._db, "s-dbg", "assistant", time.time() - 3 * _DAY_S)
    tg._pre_llm_call(session_id="s-dbg")
    assert logfile.exists()
    body = logfile.read_text()
    assert "INJECT" in body and "s-dbg" in body


def test_log_never_raises_on_bad_path(tg, monkeypatch):
    monkeypatch.setattr(tg, "_LOG_PATH", Path("/nonexistent-dir-xyz/debug.log"))
    monkeypatch.setattr(tg, "_cfg", lambda: {"debug": True})
    tg._log("boom")  # darf nicht werfen


# ── Mitternachtswechsel ───────────────────────────────────────────────────

def test_days_crossed_same_day_is_zero(tg):
    from datetime import datetime

    base = datetime(2026, 7, 13, 10, 0).timestamp()
    later = datetime(2026, 7, 13, 23, 0).timestamp()
    assert tg._days_crossed(base, later) == 0


def test_days_crossed_over_midnight(tg):
    from datetime import datetime

    before = datetime(2026, 7, 13, 23, 55).timestamp()
    after = datetime(2026, 7, 14, 0, 5).timestamp()
    assert tg._days_crossed(before, after) == 1


def test_midnight_crossing_injects_despite_small_gap(tg, monkeypatch):
    monkeypatch.setattr(tg, "_days_crossed", lambda prev, now: 1)
    _add_msg(tg._db, "s-mid", "assistant", time.time() - 120)
    result = tg._pre_llm_call(session_id="s-mid")
    assert _gap_fired(result)
    # Unsere Formulierung zeigt den Tageswechsel als "yesterday", nicht als
    # "new calendar day" — die Aussage ist dieselbe.
    assert "yesterday" in result["context"]


def test_same_day_small_gap_still_silent(tg, monkeypatch):
    monkeypatch.setattr(tg, "_days_crossed", lambda prev, now: 0)
    _add_msg(tg._db, "s-same", "assistant", time.time() - 120)
    assert not _gap_fired(tg._pre_llm_call(session_id="s-same"))


def test_large_gap_over_midnight_mentions_both(tg, monkeypatch):
    monkeypatch.setattr(tg, "_days_crossed", lambda prev, now: 1)
    _add_msg(tg._db, "s-both", "assistant", time.time() - 2 * 3600)
    result = tg._pre_llm_call(session_id="s-both")
    assert _gap_fired(result)
    assert "hours" in result["context"] and "yesterday" in result["context"]


def test_multiday_gap_omits_redundant_rollover(tg, monkeypatch):
    monkeypatch.setattr(tg, "_days_crossed", lambda prev, now: 3)
    _add_msg(tg._db, "s-multi", "assistant", time.time() - 3 * _DAY_S)
    result = tg._pre_llm_call(session_id="s-multi")
    assert _gap_fired(result)
    assert "days" in result["context"]
    assert "new calendar day" not in result["context"]


def test_date_change_trigger_can_be_disabled(tg, monkeypatch):
    monkeypatch.setattr(tg, "_cfg", lambda: {"notify_date_change": False})
    monkeypatch.setattr(tg, "_days_crossed", lambda prev, now: 1)
    _add_msg(tg._db, "s-off", "assistant", time.time() - 120)
    assert not _gap_fired(tg._pre_llm_call(session_id="s-off"))
