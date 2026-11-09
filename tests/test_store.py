"""Session-Tabelle und Arbeitszeit-Messung."""

import sqlite3
import time

from conftest import add


DAY = 86400
HOUR = 3600


# ── Session-Tabelle ───────────────────────────────────────────────────────

def test_schema_is_created(env):
    conn = env.open_session_store()
    assert conn is not None
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "sessions" in tables


def test_one_row_per_session(env):
    conn = env.open_session_store()
    now = time.time()
    env.record_session(conn, "s1", now, 0)
    env.record_session(conn, "s1", now + 10, 0)
    env.record_session(conn, "s2", now, 0)
    count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    assert count == 2


def test_write_is_throttled(env):
    conn = env.open_session_store()
    now = time.time()
    env.record_session(conn, "s1", now, 60)
    first = conn.execute("SELECT last_seen FROM sessions WHERE session_id='s1'").fetchone()[0]
    env.record_session(conn, "s1", now + 5, 60)
    second = conn.execute("SELECT last_seen FROM sessions WHERE session_id='s1'").fetchone()[0]
    conn.close()
    assert first == second


def test_latest_row_is_returned(env):
    conn = env.open_session_store()
    now = time.time()
    env.record_session(conn, "older", now - 3 * DAY, 0)
    env.record_session(conn, "newer", now - 60, 0)
    got = env.last_seen_anywhere(conn)
    conn.close()
    assert got is not None and got[0] == "newer"


def test_empty_store_returns_none(env):
    conn = env.open_session_store()
    assert env.last_seen_anywhere(conn) is None
    conn.close()


def test_global_lookup_stays_fast_with_many_sessions(env):
    """Die Tabelle wächst mit Sessions, nicht mit Nachrichten."""
    conn = env.open_session_store()
    now = time.time()
    conn.executemany(
        "INSERT INTO sessions (session_id, last_seen) VALUES (?,?)",
        [(f"s{i}", now - i) for i in range(5000)],
    )
    conn.commit()
    t0 = time.perf_counter()
    got = env.last_seen_anywhere(conn)
    elapsed = time.perf_counter() - t0
    conn.close()
    assert got is not None
    assert elapsed < 0.05, f"globale Abfrage zu langsam: {elapsed*1000:.1f} ms"


def test_broken_store_does_not_raise(env, monkeypatch, tmp_path):
    monkeypatch.setattr(env, "session_db", lambda: tmp_path / "no" / "dir" / "ta.db")
    assert env.open_session_store() is None


# ── Aktivität ─────────────────────────────────────────────────────────────

def test_activity_spans_first_and_last_user_message(env):
    now = time.time()
    add(env.db_file, "a1", "user", now - 2 * HOUR)
    add(env.db_file, "a1", "assistant", now - 2 * HOUR + 30)
    add(env.db_file, "a1", "user", now - 60)
    first, last, turns = env.session_activity("a1")
    assert turns == 2
    assert abs((last - first) - (2 * HOUR - 60)) < 5


def test_activity_none_for_unknown_session(env):
    assert env.session_activity("gibtsnicht") is None


def test_active_time_excludes_long_pauses(env):
    """Acht Stunden Spanne sind nicht acht Stunden Arbeit."""
    base = time.time() - 8 * HOUR
    add(env.db_file, "a2", "user", base)
    add(env.db_file, "a2", "user", base + 120)          # 2 min Arbeit
    add(env.db_file, "a2", "user", base + 5 * HOUR)     # 5 h Pause
    add(env.db_file, "a2", "user", base + 5 * HOUR + 180)  # 3 min Arbeit

    worked = env.active_seconds("a2")
    elapsed = 5 * HOUR + 180

    assert worked == 300, f"erwartet 300 s aktive Zeit, bekommen {worked}"
    assert worked < elapsed / 10


def test_active_time_zero_for_single_message(env):
    add(env.db_file, "a3", "user", time.time())
    assert env.active_seconds("a3") == 0.0


def test_active_time_respects_custom_break(env):
    base = time.time() - HOUR
    add(env.db_file, "a4", "user", base)
    add(env.db_file, "a4", "user", base + 300)
    assert env.active_seconds("a4", idle_break=600) == 300
    assert env.active_seconds("a4", idle_break=60) == 0.0


def test_activity_is_cached(env, monkeypatch):
    calls = {"n": 0}
    real = env.session_activity

    def counted(session_id):
        calls["n"] += 1
        return real(session_id)

    monkeypatch.setattr(env, "session_activity", counted)
    add(env.db_file, "a5", "user", time.time() - 60)
    env.cached_activity("a5", 60)
    env.cached_activity("a5", 60)
    assert calls["n"] == 1
