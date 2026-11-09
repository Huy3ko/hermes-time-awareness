"""Der Hook: was in den Turn geht und was nicht."""

import time
from datetime import datetime

from conftest import add

DAY = 86400
HOUR = 3600


def block_of(result):
    return (result or {}).get("context", "")


def has_gap(result):
    return "Last exchange:" in block_of(result)


def has_activity(result):
    return "Session:" in block_of(result)


# ── Grundverhalten ────────────────────────────────────────────────────────

def test_clock_is_always_present(env):
    out = block_of(env.on_turn(session_id="h1", platform="telegram"))
    assert out.startswith("[Temporal context]")
    assert "Now: " in out
    assert "[/Temporal context]" in out


def test_instruction_is_attached(env):
    out = block_of(env.on_turn(session_id="h2", platform="telegram"))
    assert "never quote" in out


def test_short_gap_stays_silent(env):
    add(env.db_file, "h3", "assistant", time.time() - 5 * 60)
    assert not has_gap(env.on_turn(session_id="h3", platform="telegram"))


def test_long_gap_is_reported(env):
    add(env.db_file, "h4", "assistant", time.time() - 2 * DAY)
    out = block_of(env.on_turn(session_id="h4", platform="telegram"))
    assert "Last exchange:" in out
    assert "days" in out


def test_disabled_returns_nothing(env, monkeypatch):
    monkeypatch.setattr(env, "settings", lambda: {"enabled": False})
    assert env.on_turn(session_id="h5", platform="telegram") is None


def test_broken_settings_still_returns_clock(env, monkeypatch):
    def explode():
        raise RuntimeError("kaputt")

    monkeypatch.setattr(env, "settings", explode)
    out = env.on_turn(session_id="h6", platform="telegram")
    assert out is not None and "Now: " in block_of(out)


# ── Plattform und Benachrichtigungen ──────────────────────────────────────

def test_cron_gets_clock_but_no_gap(env, monkeypatch):
    monkeypatch.setattr(env, "settings", lambda: {"exclude_platforms": ["cron"]})
    add(env.db_file, "h7", "assistant", time.time() - 5 * DAY)
    out = block_of(env.on_turn(session_id="h7", platform="cron"))
    assert "Now: " in out
    assert "Last exchange:" not in out


def test_cron_turn_does_not_write_to_store(env):
    conn = env.open_session_store()
    before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()

    env.on_turn(session_id="cron-write-probe", platform="cron")

    conn = env.open_session_store()
    after = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    assert before == after, "ein Cron-Turn darf die Gesprächsuhr nicht verschieben"


def test_telegram_turn_does_write_to_store(env):
    env.on_turn(session_id="tg-write-probe", platform="telegram")
    conn = env.open_session_store()
    row = conn.execute(
        "SELECT last_seen FROM sessions WHERE session_id='tg-write-probe'"
    ).fetchone()
    conn.close()
    assert row is not None


# ── Sessionübergreifend ───────────────────────────────────────────────────

def test_other_conversation_is_marked(env):
    conn = env.open_session_store()
    env.record_session(conn, "somewhere-else", time.time() - 22 * HOUR, 0)
    conn.close()

    out = block_of(env.on_turn(session_id="brand-new", platform="telegram"))
    assert "Last exchange:" in out
    assert "other conversation" in out
    assert "22 hours ago" in out


def test_own_history_wins_over_global(env):
    add(env.db_file, "mine", "assistant", time.time() - 60)

    conn = env.open_session_store()
    env.record_session(conn, "other", time.time() - 3 * DAY, 0)
    conn.close()

    assert not has_gap(env.on_turn(session_id="mine", platform="telegram"))


def test_own_old_history_has_no_scope_marker(env):
    add(env.db_file, "mine-old", "assistant", time.time() - 3 * DAY)
    out = block_of(env.on_turn(session_id="mine-old", platform="telegram"))
    assert "Last exchange:" in out
    assert "other conversation" not in out


# ── Mitternacht ───────────────────────────────────────────────────────────

def test_midnight_crossing_fires_below_threshold(env, monkeypatch):
    monkeypatch.setattr(env, "day_boundaries", lambda a, b: 1)
    add(env.db_file, "mid", "assistant", time.time() - 120)
    out = block_of(env.on_turn(session_id="mid", platform="telegram"))
    assert "yesterday" in out


def test_date_trigger_can_be_disabled(env, monkeypatch):
    monkeypatch.setattr(env, "settings", lambda: {"notify_date_change": False})
    monkeypatch.setattr(env, "day_boundaries", lambda a, b: 1)
    add(env.db_file, "mid-off", "assistant", time.time() - 120)
    assert not has_gap(env.on_turn(session_id="mid-off", platform="telegram"))


# ── Aktivität ─────────────────────────────────────────────────────────────

def test_activity_line_appears_for_long_session(env):
    base = time.time() - 3 * HOUR
    add(env.db_file, "long", "user", base)
    add(env.db_file, "long", "user", base + 120)
    add(env.db_file, "long", "user", base + 2 * HOUR)

    out = block_of(env.on_turn(session_id="long", platform="telegram"))
    assert "Session:" in out
    assert "active" in out
    assert "turns" in out


def test_active_time_is_marked_as_estimate(env):
    """Die aktive Zeit ist aus Nachrichtenabständen geschätzt, nicht gemessen."""
    base = time.time() - 3 * HOUR
    add(env.db_file, "est", "user", base)
    add(env.db_file, "est", "user", base + 120)          # 2 min Arbeit
    add(env.db_file, "est", "user", base + 2 * HOUR)     # lange Pause, Spanne 2 h

    out = block_of(env.on_turn(session_id="est", platform="telegram"))
    assert "Session:" in out
    assert "~" in out.split("Session:")[1].split("active")[0], \
        "die aktive Zeit muss als Schätzung erkennbar sein"


def test_activity_line_absent_for_short_session(env):
    add(env.db_file, "short", "user", time.time() - 300)
    assert not has_activity(env.on_turn(session_id="short", platform="telegram"))


def test_activity_separates_elapsed_from_worked(env):
    """Der Kern der neuen Funktion: Spanne ≠ Arbeit."""
    base = time.time() - 9 * HOUR
    add(env.db_file, "mixed", "user", base)
    add(env.db_file, "mixed", "user", base + 60)
    add(env.db_file, "mixed", "user", base + 8 * HOUR)

    out = block_of(env.on_turn(session_id="mixed", platform="telegram"))
    assert "Session:" in out
    assert "8 hours so far" in out or "9 hours so far" in out
    assert "active" in out
    # Die aktive Zeit muss deutlich kleiner sein als die Spanne.
    assert "8 hours active" not in out and "9 hours active" not in out
