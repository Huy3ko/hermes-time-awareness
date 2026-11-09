"""Sessionübergreifendes Zeitgefühl — die Ergänzung zu time-gap.

Eigene Tabelle `sessions(session_id, last_seen)`, eine Zeile pro Session.
Der Grund steht im Code: Die globale Abfrage über `messages` kostet auf dieser
Instanz 33 s (kein Index auf timestamp) und läuft in den Hook-Timeout. Die
Tabelle wächst mit der Zahl der Sessions, nicht mit der Zahl der Nachrichten.
"""

import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_DAY_S = 24 * 3600


def _make_state_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.commit()
    conn.close()


def _add_msg(path: Path, session_id: str, role: str, ts: float) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        (session_id, role, "x", ts),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def tg(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("hta_global", _PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = tmp_path / "state.db"
    _make_state_db(db)
    mod._db = db
    monkeypatch.setattr(mod, "_db_path", lambda: db)
    monkeypatch.setattr(mod, "_cfg", lambda: {})
    monkeypatch.setattr(mod, "_sessions_db_path", lambda: tmp_path / "ta.db")
    mod._query_cache.clear()
    return mod


def _gap_line(result) -> str:
    for line in result["context"].splitlines():
        if line.startswith("Last exchange:"):
            return line
    return ""


def test_new_session_knows_last_global_exchange(tg, monkeypatch):
    """Frische Session, keine eigene Historie → globale Lücke, markiert."""
    # Eintrag einer älteren Session, 22 Stunden alt
    conn = tg._open_sessions_db()
    conn.execute(
        "INSERT INTO sessions (session_id, last_seen) VALUES (?, ?)",
        ("old-session", time.time() - 22 * 3600),
    )
    conn.commit()
    conn.close()

    result = tg._pre_llm_call(session_id="brand-new", platform="telegram")
    line = _gap_line(result)
    assert line, "die globale Lücke muss erscheinen"
    assert "22 hours ago" in line
    assert "other conversation" in line


def test_own_history_wins_over_global(tg):
    """Hat die Session eigene Nachrichten, gilt deren Zeit — nicht die globale."""
    now = time.time()
    _add_msg(tg._db, "mine", "assistant", now - 60)          # 1 min → unter Schwelle

    conn = tg._open_sessions_db()
    conn.execute(
        "INSERT INTO sessions (session_id, last_seen) VALUES (?, ?)",
        ("other", now - 3 * _DAY_S),
    )
    conn.commit()
    conn.close()

    result = tg._pre_llm_call(session_id="mine", platform="telegram")
    assert not _gap_line(result), "die eigene frische Historie gewinnt"


def test_own_old_history_fires_without_scope_marker(tg):
    _add_msg(tg._db, "mine-old", "assistant", time.time() - 3 * _DAY_S)
    result = tg._pre_llm_call(session_id="mine-old", platform="telegram")
    line = _gap_line(result)
    assert line and "days ago" in line
    assert "other conversation" not in line


def test_touch_writes_one_row_per_session(tg):
    conn = tg._open_sessions_db()
    now = time.time()
    tg._touch_session(conn, "s1", now, 0)
    tg._touch_session(conn, "s1", now + 10, 0)
    tg._touch_session(conn, "s2", now, 0)
    rows = conn.execute("SELECT session_id, last_seen FROM sessions ORDER BY session_id").fetchall()
    conn.close()
    assert len(rows) == 2, "eine Zeile pro Session, nicht eine pro Turn"


def test_touch_is_throttled(tg):
    conn = tg._open_sessions_db()
    now = time.time()
    tg._touch_session(conn, "s1", now, 60)
    first = conn.execute("SELECT last_seen FROM sessions WHERE session_id='s1'").fetchone()[0]
    tg._touch_session(conn, "s1", now + 5, 60)   # innerhalb TTL → kein Schreibvorgang
    second = conn.execute("SELECT last_seen FROM sessions WHERE session_id='s1'").fetchone()[0]
    conn.close()
    assert first == second


def test_broken_sessions_db_does_not_break_turn(tg, monkeypatch):
    """Kaputte Session-DB → Uhr läuft weiter, keine Ausnahme."""
    monkeypatch.setattr(tg, "_sessions_db_path", lambda: Path("/nonexistent-dir-xyz/ta.db"))
    _add_msg(tg._db, "s", "assistant", time.time() - 5 * _DAY_S)
    result = tg._pre_llm_call(session_id="s", platform="telegram")
    assert result is not None and "Now: " in result["context"]


def test_global_query_costs_nothing_on_large_table(tg):
    """Die globale Abfrage bleibt billig, auch bei vielen Sessions."""
    conn = tg._open_sessions_db()
    now = time.time()
    conn.executemany(
        "INSERT INTO sessions (session_id, last_seen) VALUES (?, ?)",
        [(f"s{i}", now - i) for i in range(5000)],
    )
    conn.commit()
    t0 = time.perf_counter()
    got = tg._last_global_exchange(conn)
    dt = time.perf_counter() - t0
    conn.close()
    assert got is not None
    assert dt < 0.05, f"globale Abfrage zu langsam: {dt*1000:.1f} ms"
