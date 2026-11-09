"""hermes-time-awareness — absolute and relative time in one turn.

Zwei Zeitdimensionen, eine Injektion:

  NOW      absolute Zeit — Systemuhr, kein I/O. Jeder Turn.
  ELAPSED  relative Zeit — Lücke seit dem letzten Austausch. Nur wenn sie
           bedeutsam ist (Schwelle oder Kalenderwechsel).

Basis: time-gap v2.1.0 (Randool, keine Lizenz deklariert). Übernommen sind
Datenbankzugriff, WAL-Behandlung, Session-Erkennung, Notification-Filter,
Zeitzonen-Auflösung über Hermes' eigenes `hermes_time`, Fail-open und die
Formulierung. Neu: der Uhr-Teil, ein gemeinsamer Block und ein Abfrage-Cache.

Der Hook hängt am User-Turn, nie am System-Prompt — der Prompt-Cache bleibt
byte-stabil. Nichts wird geschrieben; keine Datei, kein Memory, kein Zustand.
Einzige Ausnahme ist `debug.log` bei `debug: true`.
"""

import json
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

_MINUTE, _HOUR, _DAY = 60, 3600, 86400
_DEFAULT_TZ = "Europe/Berlin"
_DEFAULT_MIN_GAP_SECONDS = 120 * _MINUTE
_DEFAULT_EXCLUDE_PLATFORMS = ("cron",)
_DEFAULT_QUERY_TTL = 60.0
_LOG_PATH = Path(__file__).resolve().parent / "debug.log"

_PERIODS = ((12, "morning"), (14, "midday"), (18, "afternoon"), (22, "evening"))

_config_cache: Optional[Dict[str, Any]] = None
# (session_id) -> (prev_ts|None, checked_monotonic)
_query_cache: Dict[str, Tuple[Optional[float], float]] = {}


# ── Registrierung ──────────────────────────────────────────────────────────

def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _pre_llm_call)


# ── Konfiguration ──────────────────────────────────────────────────────────

def _cfg() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is None:
        try:
            from hermes_cli.config import load_config

            entries = ((load_config() or {}).get("plugins") or {}).get("entries") or {}
            _config_cache = entries.get("hermes-time-awareness") or {}
        except Exception:
            _config_cache = {}
    return _config_cache


def _log(msg: str) -> None:
    try:
        if not _cfg().get("debug"):
            return
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass  # ein kaputter Log-Pfad darf keinen Turn brechen


def _min_gap_seconds(cfg: Dict[str, Any]) -> float:
    try:
        val = float(cfg.get("min_gap_seconds", _DEFAULT_MIN_GAP_SECONDS))
        return val if val > 0 else _DEFAULT_MIN_GAP_SECONDS
    except (TypeError, ValueError):
        return _DEFAULT_MIN_GAP_SECONDS


def _query_ttl(cfg: Dict[str, Any]) -> float:
    try:
        val = float(cfg.get("query_ttl_seconds", _DEFAULT_QUERY_TTL))
        return val if val >= 0 else _DEFAULT_QUERY_TTL
    except (TypeError, ValueError):
        return _DEFAULT_QUERY_TTL


def _excluded(platform: str, cfg: Dict[str, Any]) -> bool:
    try:
        raw = cfg.get("exclude_platforms", _DEFAULT_EXCLUDE_PLATFORMS)
        return (platform or "").strip().lower() in {str(p).strip().lower() for p in raw}
    except TypeError:
        return (platform or "").strip().lower() in _DEFAULT_EXCLUDE_PLATFORMS


# ── Zeit ───────────────────────────────────────────────────────────────────

def _now(tz_name: str = _DEFAULT_TZ):
    try:
        return datetime.now(ZoneInfo(tz_name)), tz_name
    except Exception:
        # Echter UTC-Anker, nie eine unbenannte lokale Vermutung.
        return datetime.now(timezone.utc), "UTC"


def _day_period(hour: int) -> str:
    if hour >= 22 or hour < 6:
        return "night"
    for limit, name in _PERIODS:
        if hour < limit:
            return name
    return "night"


def _offset_fmt(dt: datetime) -> str:
    off = dt.strftime("%z") or "+0000"
    return f"{off[:3]}:{off[3:]}" if len(off) == 5 else off


def _local_date(epoch: float):
    try:
        from hermes_time import get_timezone

        return datetime.fromtimestamp(epoch, get_timezone()).date()
    except Exception:
        return datetime.fromtimestamp(epoch).astimezone().date()


def _days_crossed(prev: float, now: float) -> int:
    return (_local_date(now) - _local_date(prev)).days


def _humanize(gap: float) -> str:
    if gap >= _DAY:
        days = round(gap / _DAY)
        return "a day" if days <= 1 else f"{days} days"
    if gap >= _HOUR:
        hours = round(gap / _HOUR)
        if hours >= 24:
            return "a day"
        return "an hour" if hours <= 1 else f"{hours} hours"
    minutes = round(gap / _MINUTE)
    return "a minute" if minutes <= 1 else f"{minutes} minutes"


def _relative_label(prev: float, now: float) -> str:
    days = _days_crossed(prev, now)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


# ── Datenbank ──────────────────────────────────────────────────────────────

def _db_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state.db"


# ── Kleine Session-Tabelle (sessionübergreifendes Zeitgefühl) ──────────────
#
# Warum eine eigene Tabelle: Die globale Abfrage
#   SELECT timestamp FROM messages ORDER BY timestamp DESC LIMIT 1
# kostet auf dieser Instanz 33 s (kein Index auf timestamp, 851k Zeilen) und
# läuft damit in den 30-s-Hook-Timeout — sie liefert nie ein Ergebnis.
#
# Deshalb eine Zeile pro Session, geschrieben beim Turn (gedrosselt):
#   sessions(session_id PRIMARY KEY, last_seen REAL)
# Der jüngste Eintrag ist der letzte Austausch überhaupt. Die Tabelle wächst mit
# der Zahl der Sessions, nicht mit der Zahl der Nachrichten.

_SESSION_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    last_seen  REAL NOT NULL
)
"""
_SESSION_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen DESC)"
)


def _sessions_db_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "time-awareness.db"


def _open_sessions_db():
    """Eigene DB, eigenes Schema. Gibt None zurück statt zu werfen."""
    try:
        path = _sessions_db_path()
        conn = sqlite3.connect(str(path), timeout=1.0)
        conn.execute(_SESSION_TABLE)
        conn.execute(_SESSION_INDEX)
        return conn
    except Exception:
        _log(f"sessions db open failed:\n{traceback.format_exc()}")
        return None


def _last_global_exchange(conn) -> Optional[Tuple[str, float]]:
    """Jüngster Eintrag über alle Sessions — (session_id, last_seen)."""
    try:
        row = conn.execute(
            "SELECT session_id, last_seen FROM sessions ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
        if row and isinstance(row[1], (int, float)) and row[1] > 0:
            return row[0], float(row[1])
        return None
    except Exception:
        return None


def _touch_session(conn, session_id: str, now: float, ttl: float) -> None:
    """Eine Zeile pro Session, höchstens einmal pro TTL geschrieben."""
    if not session_id:
        return
    try:
        row = conn.execute(
            "SELECT last_seen FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row and isinstance(row[0], (int, float)) and (now - row[0]) < ttl:
            return
        conn.execute(
            "INSERT INTO sessions (session_id, last_seen) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET last_seen = excluded.last_seen",
            (session_id, now),
        )
        conn.commit()
    except Exception:
        _log(f"touch failed:\n{traceback.format_exc()}")


def _notification(content: Any, display_kind: Any = None,
                  display_metadata: Any = None) -> bool:
    """Erkennt ausdrückliche Notification-Marker, rät nicht aus Prosa."""
    if display_kind == "notification":
        return True
    if isinstance(display_metadata, str):
        try:
            display_metadata = json.loads(display_metadata)
        except (ValueError, TypeError):
            display_metadata = None
    if isinstance(display_metadata, dict) and display_metadata.get("notification_category"):
        return True
    prefixes = _cfg().get("exclude_user_prefixes", [])
    if not isinstance(prefixes, (list, tuple)) or not prefixes:
        return False
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, list):
            content = decoded
    if isinstance(content, list):
        content = "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text")
            and isinstance(part.get("text"), str)
        )
    return isinstance(content, str) and any(
        isinstance(p, str) and bool(p) and content.startswith(p) for p in prefixes
    )


def _last_exchange_ts(session_id: str) -> Optional[float]:
    """Letzter Assistant-Zeitstempel außerhalb von Notification-Turns.

    Rückwärts in persistierter Reihenfolge: eine User-Zeile besitzt alle
    Assistant-Zeilen bis zur nächsten User-Zeile, Tool-Calls eingeschlossen.
    `session_id` ist der erste Schlüssel von `idx_messages_session` — die
    Abfrage kostet damit Millisekunden statt eines Tabellenscans.
    """
    path = _db_path()
    if not session_id or not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=1.0
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        kind = "display_kind" if "display_kind" in columns else "NULL"
        metadata = "display_metadata" if "display_metadata" in columns else "NULL"
        rows = conn.execute(
            f"SELECT role, content, timestamp, {kind}, {metadata} FROM messages "
            "WHERE session_id = ? AND role IN ('user', 'assistant') "
            "ORDER BY rowid DESC",
            (session_id,),
        )
        candidate = None
        for role, content, ts, dk, dm in rows:
            marked = _notification(content if role == "user" else None, dk, dm)
            if role == "assistant":
                if not marked and isinstance(ts, (int, float)) and ts > 0:
                    candidate = max(candidate or ts, ts)
            elif marked:
                candidate = None
            elif candidate is not None:
                return candidate
        return candidate
    except Exception:
        _log(f"DB read failed:\n{traceback.format_exc()}")
        return None
    finally:
        if conn is not None:
            conn.close()


def _cached_last_exchange(session_id: str, ttl: float) -> Optional[float]:
    """Dieselbe Abfrage höchstens einmal pro TTL — nicht bei jedem Turn."""
    now = time.monotonic()
    hit = _query_cache.get(session_id)
    if hit is not None and (now - hit[1]) < ttl:
        return hit[0]
    prev = _last_exchange_ts(session_id)
    _query_cache[session_id] = (prev, now)
    return prev


# ── Kontext ────────────────────────────────────────────────────────────────

def _build_context(prev: Optional[float], now_ts: float,
                   include_gap: bool, same_session: bool = True) -> str:
    dt, tz_name = _now()
    parts = [
        "[Temporal context]",
        f"Now: {dt.strftime('%Y-%m-%d %H:%M:%S')} · {tz_name} · "
        f"{dt.strftime('%A')} · {_day_period(dt.hour)}",
    ]
    if include_gap and prev is not None:
        prev_dt = datetime.fromtimestamp(prev, dt.tzinfo)
        gap = now_ts - prev
        scope = "" if same_session else " · other conversation"
        parts.append(
            f"Last exchange: {prev_dt.strftime('%Y-%m-%d %H:%M')} · "
            f"{_humanize(gap)} ago · {_relative_label(prev, now_ts)}{scope}"
        )
    parts.append("[/Temporal context]")
    parts.append(
        "(Your own sense of time — never quote or mention this block. "
        "Factor it in silently: earlier context or dates may be stale; "
        "re-ground if needed.)"
    )
    return "\n".join(parts)


def _pre_llm_call(*, session_id: str = "", platform: str = "",
                  user_message: Any = None, **_: Any):
    """Clock immer; Lücke nur, wenn sie etwas zu sagen hat.

    Reihenfolge ist wichtig: erst lesen, dann schreiben. Sonst überschreibt der
    eigene Turn den Zeitpunkt, den man gerade messen wollte.
    """
    try:
        cfg = _cfg()
        if cfg.get("enabled", True) is False:
            return None

        now_ts = time.time()
        include_gap = False
        prev = None
        same_session = True
        skip = _excluded(platform, cfg) or _notification(user_message)

        conn = None if skip else _open_sessions_db()
        try:
            if not skip:
                # Lokale Sicht zuerst: Wie alt ist der Kontext, den ich sehe?
                local = _cached_last_exchange(session_id, _query_ttl(cfg))
                if local is not None:
                    prev = local
                    same_session = True
                else:
                    # Neue Session ohne eigene Historie: wann haben wir zuletzt
                    # überhaupt gesprochen? Das ist der eigentliche Gewinn dieser
                    # Tabelle — sie kostet nichts, weil sie eine Zeile pro
                    # Session hält statt 851k Nachrichten zu durchsuchen.
                    global_exchange = _last_global_exchange(conn) if conn else None
                    if global_exchange is not None:
                        _, prev = global_exchange
                        same_session = False

                if prev is not None:
                    elapsed = now_ts - prev
                    days = 0 if cfg.get("notify_date_change", True) is False \
                        else _days_crossed(prev, now_ts)
                    include_gap = elapsed >= _min_gap_seconds(cfg) or days >= 1

                _touch_session(conn, session_id, now_ts, _query_ttl(cfg))
        finally:
            if conn is not None:
                conn.close()

        context = _build_context(prev, now_ts, include_gap, same_session)
        _log(f"INJECT session={session_id[:16]} gap={include_gap} same={same_session}")
        return {"context": context}
    except Exception:
        _log(f"error:\n{traceback.format_exc()}")
        try:
            return {"context": _build_context(None, time.time(), False)}
        except Exception:
            return None
