"""Lesezugriff auf state.db: letzter Austausch, Aktivität, Sonderfälle."""

import sqlite3
import time

from conftest import add


DAY = 86400


# ── letzter abgeschlossener Austausch ─────────────────────────────────────

def test_no_previous_exchange_without_assistant(env):
    add(env.db_file, "s1", "user", time.time())
    assert env.last_completed_exchange("s1") is None


def test_missing_database_is_quiet(env, monkeypatch, tmp_path):
    monkeypatch.setattr(env, "message_db", lambda: tmp_path / "nope.db")
    assert env.last_completed_exchange("s1") is None


def test_recent_exchange_is_returned(env):
    now = time.time()
    add(env.db_file, "s2", "assistant", now - 300)
    got = env.last_completed_exchange("s2")
    assert got is not None and abs(got - (now - 300)) < 2


def test_newest_assistant_wins(env):
    now = time.time()
    add(env.db_file, "s3", "assistant", now - 5 * DAY)
    add(env.db_file, "s3", "assistant", now - 600)
    got = env.last_completed_exchange("s3")
    assert got is not None and abs(got - (now - 600)) < 2


def test_open_user_turn_does_not_reset_the_clock(env):
    """Der laufende Turn ist eine User-Zeile — er darf den Abschluss nicht verdecken."""
    now = time.time()
    add(env.db_file, "s4", "assistant", now - 2 * DAY)
    add(env.db_file, "s4", "user", now)
    got = env.last_completed_exchange("s4")
    assert got is not None and abs(got - (now - 2 * DAY)) < 5


def test_sessions_do_not_bleed(env):
    now = time.time()
    add(env.db_file, "old", "assistant", now - 3 * DAY)
    add(env.db_file, "fresh", "assistant", now - 60)

    fresh = env.last_completed_exchange("fresh")
    old = env.last_completed_exchange("old")

    assert fresh is not None and abs(fresh - (now - 60)) < 2
    assert old is not None and abs(old - (now - 3 * DAY)) < 5


def test_empty_session_id_returns_none(env):
    assert env.last_completed_exchange("") is None


def test_uncheckpointed_wal_row_is_visible(env):
    """Der wichtigste Test: eine committete, nicht gecheckpointete Zeile.

    Hermes schreibt im WAL-Modus. Wird eine gerade beendete Antwort noch nicht
    in die Hauptdatei geschrieben, muss eine Nur-Lese-Verbindung sie trotzdem
    sehen — sonst fehlt der Hinweis genau beim Wiedereinstieg nach einem Turn.
    """
    writer = sqlite3.connect(str(env.db_file))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        ("wal", "assistant", "x", time.time() - 3 * DAY),
    )
    writer.commit()
    try:
        got = env.last_completed_exchange("wal")
        assert got is not None
    finally:
        writer.close()


# ── Notification-Filter ───────────────────────────────────────────────────

def test_notification_assistant_row_is_ignored(env, monkeypatch):
    """Eine Benachrichtigungs-Antwort zählt nicht als Gesprächsende."""
    now = time.time()
    # Echter Austausch vor zwei Tagen, danach eine Benachrichtigung.
    add(env.db_file, "n1", "user", now - 2 * DAY)
    add(env.db_file, "n1", "assistant", now - 2 * DAY + 30)
    add(env.db_file, "n1", "user", now - 300)
    conn = sqlite3.connect(str(env.db_file))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) "
        "VALUES (?,?,?,?,?)",
        ("n1", "assistant", "x", now - 60, "notification"),
    )
    conn.commit()
    conn.close()

    got = env.last_completed_exchange("n1")
    assert got is not None
    # Der echte Abschluss liegt zwei Tage zurück, nicht bei der Notification.
    assert env.day_boundaries(got, now) >= 1


def test_old_schema_without_display_columns(env, monkeypatch, tmp_path):
    """Ältere state.db ohne display_kind/display_metadata darf nicht brechen."""
    old = tmp_path / "old" / "state.db"
    old.parent.mkdir()
    conn = sqlite3.connect(str(old))
    conn.execute(
        "CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(env, "message_db", lambda: old)

    now = time.time()
    add(old, "legacy", "assistant", now - 3 * DAY)
    got = env.last_completed_exchange("legacy")
    assert got is not None


def test_prefix_marks_automatic_turn(env, monkeypatch):
    monkeypatch.setattr(env, "settings", lambda: {"exclude_user_prefixes": ["[auto "]})
    assert env.is_automatic("[auto timer] los", None, None) is True
    assert env.is_automatic("normale Nachricht", None, None) is False


def test_notification_category_in_metadata(env):
    assert env.is_automatic("x", None, '{"notification_category":"reminder"}') is True
    assert env.is_automatic("x", None, '{"other":"y"}') is False


def test_display_kind_notification(env):
    assert env.is_automatic("x", "notification", None) is True
    assert env.is_automatic("x", "message", None) is False


def test_multimodal_prefix_match(env, monkeypatch):
    """Prefix-Prüfung muss auch in JSON-kodierten Textblöcken greifen."""
    monkeypatch.setattr(env, "settings", lambda: {"exclude_user_prefixes": ["[auto "]})
    payload = '[{"type":"text","text":"[auto job] start"}]'
    assert env.is_automatic(payload, None, None) is True
