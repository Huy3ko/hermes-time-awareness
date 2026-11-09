"""hermes-time-awareness — a sense of time for Hermes agents.

One hook, one injected block, four questions from three sources:

    what time is it?                  system clock                    ~13 µs
    how long since we spoke?          state.db, keyed by session      ~16 ms, cached
    when did we last speak at all?    session table                   ~0.05 ms
    how long has this session run?    state.db, cached

The block stays silent about everything it does not need to say. A continuous
conversation gets one line. A gap gets two. A long session gets three.

Nothing is written to messages, sessions or memory. The only file is
`time-awareness.db`: one row per conversation, no content.
"""

from __future__ import annotations

import json
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# ── Konstanten ─────────────────────────────────────────────────────────────

SECOND = 1
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR

CONFIG_KEY = "hermes-time-awareness"
DEFAULT_ZONE = "Europe/Berlin"
DEFAULT_GAP = 2 * HOUR
DEFAULT_CACHE_SECONDS = 60.0
DEFAULT_IDLE_BREAK = 10 * MINUTE      # länger = Pause, nicht Arbeit
DEFAULT_EXCLUDED = ("cron",)
LOOKBACK_ROWS = 400          # Rückwärtssuche je Turn begrenzt halten

_DEBUG_LOG = Path(__file__).resolve().parent / "debug.log"

# Tagesabschnitt: halboffene Intervalle, 22–06 als Mitternachtsumbruch.
PHASES: Tuple[Tuple[int, str], ...] = (
    (6, "morning"),
    (12, "midday"),
    (14, "afternoon"),
    (18, "evening"),
)

_config: Optional[Dict[str, Any]] = None
_history_cache: Dict[str, Tuple[Optional[float], float]] = {}
_activity_cache: Dict[str, Tuple[Optional[Tuple[float, float, int]], float]] = {}


# ── Registrierung ──────────────────────────────────────────────────────────

def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_turn)


# ── Konfiguration ──────────────────────────────────────────────────────────

def settings() -> Dict[str, Any]:
    global _config
    if _config is None:
        try:
            from hermes_cli.config import load_config

            blocks = ((load_config() or {}).get("plugins") or {}).get("entries") or {}
            _config = blocks.get(CONFIG_KEY) or {}
        except Exception:
            _config = {}
    return _config


def trace(message: str) -> None:
    try:
        if not settings().get("debug"):
            return
        with open(_DEBUG_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def _positive(cfg: Dict[str, Any], key: str, fallback: float) -> float:
    raw = cfg.get(key, fallback)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _cache_seconds(cfg: Dict[str, Any]) -> float:
    raw = cfg.get("query_ttl_seconds", DEFAULT_CACHE_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CACHE_SECONDS
    return max(0.0, value)


def _excluded(platform: str, cfg: Dict[str, Any]) -> bool:
    listed = cfg.get("exclude_platforms", DEFAULT_EXCLUDED)
    try:
        names = {str(item).strip().lower() for item in listed}
    except TypeError:
        names = set(DEFAULT_EXCLUDED)
    return (platform or "").strip().lower() in names


# ── Pfade ──────────────────────────────────────────────────────────────────

def _home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def message_db() -> Path:
    return _home() / "state.db"


def session_db() -> Path:
    return _home() / "time-awareness.db"


# ── Zeit ───────────────────────────────────────────────────────────────────

def now_in(zone: str = DEFAULT_ZONE) -> Tuple[datetime, str]:
    """Aktuelle Zeit. Fällt auf echtes UTC zurück, nie auf eine stille Annahme."""
    try:
        return datetime.now(ZoneInfo(zone)), zone
    except Exception:
        return datetime.now(timezone.utc), "UTC"


def phase_of(hour: int) -> str:
    """Tagesabschnitt aus der Stunde — deterministisch, Grenzen exakt."""
    if hour < 6 or hour >= 22:
        return "night"
    current = "night"
    for start, name in PHASES:
        if hour >= start:
            current = name
    return current


def utc_offset(moment: datetime) -> str:
    raw = moment.strftime("%z") or "+0000"
    return f"{raw[:3]}:{raw[3:]}" if len(raw) == 5 else raw


def _as_date(epoch: float):
    """Kalendertag eines Zeitpunkts in der konfigurierten Zone."""
    try:
        from hermes_time import get_timezone

        return datetime.fromtimestamp(epoch, get_timezone()).date()
    except Exception:
        return datetime.fromtimestamp(epoch).astimezone().date()


def day_boundaries(earlier: float, later: float) -> int:
    """Anzahl Kalendergrenzen zwischen zwei Zeitpunkten."""
    return (_as_date(later) - _as_date(earlier)).days


# ── Formulierung ───────────────────────────────────────────────────────────

_HUMAN_STEPS: Tuple[Tuple[float, str, str], ...] = (
    (DAY, "day", "days"),
    (HOUR, "hour", "hours"),
    (MINUTE, "minute", "minutes"),
)


def describe_duration(seconds: float) -> str:
    """Grobe Dauer. Nie exakt — „2 hours" statt „2 h 07 min"."""
    if seconds >= DAY:
        count = int(round(seconds / DAY))
        return "a day" if count <= 1 else f"{count} days"
    for size, single, many in _HUMAN_STEPS:
        if seconds >= size:
            count = int(round(seconds / size))
            if size == HOUR and count >= 24:
                return "a day"          # 23,6 h runden nicht auf "24 hours"
            if count > 1:
                return f"{count} {many}"
            return "an hour" if single == "hour" else f"a {single}"
    return "a minute"


def describe_distance(earlier: float, later: float) -> str:
    """Wie der Zeitpunkt relativ zum Jetzt heißt."""
    days = day_boundaries(earlier, later)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


# ── Lesezugriff: state.db ──────────────────────────────────────────────────

def _read_only(path: Path) -> Optional[sqlite3.Connection]:
    try:
        return sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=1.0
        )
    except Exception:
        trace(f"open failed: {path}")
        return None


def _columns(conn: sqlite3.Connection) -> set:
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    except Exception:
        return set()


def _text_of(content: Any) -> str:
    """Nur den Text aus einem Nachrichteninhalt ziehen, multimodal inklusive."""
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                return content
            if isinstance(parsed, list):
                return _text_of(parsed)
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                if part.get("type") in ("text", "input_text"):
                    pieces.append(part["text"])
        return "\n".join(pieces)
    return ""


def _metadata_flags(meta: Any) -> Dict[str, Any]:
    if isinstance(meta, str):
        try:
            loaded = json.loads(meta)
        except (ValueError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return meta if isinstance(meta, dict) else {}


def is_automatic(content: Any, kind: Any, meta: Any) -> bool:
    """Automatisierte Nachricht? Nur an ausdrücklichen Merkmalen erkennen."""
    if kind == "notification":
        return True
    if _metadata_flags(meta).get("notification_category"):
        return True

    prefixes = settings().get("exclude_user_prefixes") or []
    if not isinstance(prefixes, (list, tuple)) or not prefixes:
        return False

    text = _text_of(content)
    if not text:
        return False
    return any(isinstance(p, str) and p and text.startswith(p) for p in prefixes)


def last_completed_exchange(session_id: str) -> Optional[float]:
    """Zeitpunkt des letzten abgeschlossenen Austauschs dieser Session.

    Rückwärts durch die persistierten Zeilen: eine User-Zeile besitzt alles bis
    zur nächsten User-Zeile (Tool-Aufrufe eingeschlossen). Der erste Assistant-
    Zeitstempel nach dem Überschreiten einer User-Zeile ist der Abschluss des
    vorherigen Austauschs — der aktuelle Turn zählt nicht mit.

    `session_id` ist der führende Schlüssel von `idx_messages_session`. Ohne ihn
    wird die Abfrage zum Tabellenscan über die gesamte Nachrichtenhistorie.
    """
    path = message_db()
    if not session_id or not path.exists():
        return None

    conn = _read_only(path)
    if conn is None:
        return None

    try:
        cols = _columns(conn)
        kind_expr = "display_kind" if "display_kind" in cols else "NULL"
        meta_expr = "display_metadata" if "display_metadata" in cols else "NULL"
        # Begrenzt: der Abschluss liegt praktisch immer in den letzten Zeilen.
        # Ohne Limit kostet eine lange Session mit vielen Tool-Aufrufen ~0,4 s.
        rows = conn.execute(
            f"SELECT role, content, timestamp, {kind_expr}, {meta_expr} "
            "FROM messages WHERE session_id = ? AND role IN ('user','assistant') "
            "ORDER BY rowid DESC LIMIT ?",
            (session_id, LOOKBACK_ROWS),
        )

        finished_at: Optional[float] = None
        for role, content, ts, kind, meta in rows:
            if role == "assistant":
                if isinstance(ts, (int, float)) and ts > 0:
                    if not is_automatic(None, kind, meta):
                        finished_at = ts if finished_at is None else max(finished_at, ts)
                continue

            if is_automatic(content, kind, meta):
                finished_at = None
            elif finished_at is not None:
                return finished_at

        return finished_at
    except Exception:
        trace(f"read failed:\n{traceback.format_exc()}")
        return None
    finally:
        conn.close()


def session_activity(session_id: str) -> Optional[Tuple[float, float, int]]:
    """(erste User-Nachricht, letzte User-Nachricht, Anzahl) dieser Session."""
    path = message_db()
    if not session_id or not path.exists():
        return None

    conn = _read_only(path)
    if conn is None:
        return None

    try:
        row = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM messages "
            "WHERE session_id = ? AND role = 'user'",
            (session_id,),
        ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None
        return float(row[0]), float(row[1]), int(row[2] or 0)
    except Exception:
        return None
    finally:
        conn.close()


def active_seconds(session_id: str, idle_break: float = DEFAULT_IDLE_BREAK) -> float:
    """Summe der Abstände zwischen User-Nachrichten, Pausen herausgerechnet.

    Der Kern: Acht Stunden zwischen erster und letzter Nachricht sind nicht acht
    Stunden Arbeit. Jeder Abstand über `idle_break` gilt als Abwesenheit.
    """
    path = message_db()
    if not session_id or not path.exists():
        return 0.0

    conn = _read_only(path)
    if conn is None:
        return 0.0

    try:
        stamps = [
            float(r[0])
            for r in conn.execute(
                "SELECT timestamp FROM messages WHERE session_id = ? "
                "AND role = 'user' ORDER BY timestamp",
                (session_id,),
            )
            if isinstance(r[0], (int, float))
        ]
    except Exception:
        return 0.0
    finally:
        conn.close()

    total = 0.0
    for earlier, later in zip(stamps, stamps[1:]):
        step = later - earlier
        if 0 < step <= idle_break:
            total += step
    return total


# ── Lesezugriff: session-Tabelle ───────────────────────────────────────────

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS sessions ("
    "session_id TEXT PRIMARY KEY, last_seen REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen DESC)",
)


def open_session_store() -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(str(session_db()), timeout=1.0)
        for statement in _SCHEMA:
            conn.execute(statement)
        return conn
    except Exception:
        trace(f"session store failed:\n{traceback.format_exc()}")
        return None


def last_seen_anywhere(conn: sqlite3.Connection) -> Optional[Tuple[str, float]]:
    try:
        row = conn.execute(
            "SELECT session_id, last_seen FROM sessions ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
        if row and isinstance(row[1], (int, float)) and row[1] > 0:
            return str(row[0]), float(row[1])
        return None
    except Exception:
        return None


def record_session(conn: sqlite3.Connection, session_id: str,
                   moment: float, min_age: float) -> None:
    """Eine Zeile pro Session. Geschrieben wird nur, wenn sie veraltet ist."""
    if not session_id:
        return
    try:
        row = conn.execute(
            "SELECT last_seen FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row and isinstance(row[0], (int, float)) and (moment - row[0]) < min_age:
            return
        conn.execute(
            "INSERT INTO sessions (session_id, last_seen) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET last_seen = excluded.last_seen",
            (session_id, moment),
        )
        conn.commit()
    except Exception:
        trace(f"record failed:\n{traceback.format_exc()}")


# ── Cache ──────────────────────────────────────────────────────────────────

def cached_exchange(session_id: str, seconds: float) -> Optional[float]:
    stamp = time.monotonic()
    hit = _history_cache.get(session_id)
    if hit and (stamp - hit[1]) < seconds:
        return hit[0]
    value = last_completed_exchange(session_id)
    _history_cache[session_id] = (value, stamp)
    return value


def cached_activity(session_id: str, seconds: float) -> Optional[Tuple[float, float, int]]:
    stamp = time.monotonic()
    hit = _activity_cache.get(session_id)
    if hit and (stamp - hit[1]) < seconds:
        return hit[0]
    value = session_activity(session_id)
    _activity_cache[session_id] = (value, stamp)
    return value


# ── Block ──────────────────────────────────────────────────────────────────

def render(now_ts: float, previous: Optional[float], show_gap: bool,
           cross_session: bool, activity: Optional[Tuple[float, float, int]],
           worked: float = 0.0) -> str:
    moment, zone = now_in()
    lines: List[str] = ["[Temporal context]"]
    lines.append(
        f"Now: {moment.strftime('%Y-%m-%d %H:%M:%S')} · {zone} · "
        f"{moment.strftime('%A')} · {phase_of(moment.hour)}"
    )

    if show_gap and previous is not None:
        then = datetime.fromtimestamp(previous, moment.tzinfo)
        scope = " · other conversation" if cross_session else ""
        lines.append(
            f"Last exchange: {then.strftime('%Y-%m-%d %H:%M')} · "
            f"{describe_duration(now_ts - previous)} ago · "
            f"{describe_distance(previous, now_ts)}{scope}"
        )

    if activity is not None:
        first, last, turns = activity
        elapsed = last - first
        if elapsed >= HOUR:
            lines.append(
                f"Session: {describe_duration(elapsed)} so far · "
                f"{describe_duration(worked)} active · {turns} turns"
            )

    lines.append("[/Temporal context]")
    lines.append(
        "(Your own sense of time — never quote or mention this block. "
        "Factor it in silently: earlier context or dates may be stale; "
        "re-ground if needed.)"
    )
    return "\n".join(lines)


def on_turn(*, session_id: str = "", platform: str = "",
            user_message: Any = None, **_: Any):
    """Clock immer. Lücke, wenn sie zählt. Aktivität, wenn die Session lange läuft."""
    try:
        cfg = settings()
        if cfg.get("enabled", True) is False:
            return None

        stamp = time.time()
        show_gap = False
        previous: Optional[float] = None
        cross_session = False
        activity: Optional[Tuple[float, float, int]] = None
        worked = 0.0
        quiet = _excluded(platform, cfg) or is_automatic(user_message, None, None)

        store = None if quiet else open_session_store()
        try:
            if not quiet:
                own = cached_exchange(session_id, _cache_seconds(cfg))
                if own is not None:
                    previous = own
                elif store is not None:
                    elsewhere = last_seen_anywhere(store)
                    if elsewhere is not None:
                        _, previous = elsewhere
                        cross_session = True

                if previous is not None:
                    gap = stamp - previous
                    rolled = (0 if cfg.get("notify_date_change", True) is False
                              else day_boundaries(previous, stamp))
                    show_gap = gap >= _positive(cfg, "min_gap_seconds", DEFAULT_GAP) \
                        or rolled >= 1

                activity = cached_activity(session_id, _cache_seconds(cfg))
                if activity is not None and (activity[1] - activity[0]) >= HOUR:
                    worked = active_seconds(
                        session_id,
                        _positive(cfg, "idle_break_seconds", DEFAULT_IDLE_BREAK),
                    )

                if store is not None:
                    record_session(store, session_id, stamp, _cache_seconds(cfg))
        finally:
            if store is not None:
                store.close()

        block = render(stamp, previous, show_gap, cross_session, activity, worked)
        trace(f"turn session={session_id[:16]} gap={show_gap} other={cross_session}")
        return {"context": block}

    except Exception:
        trace(f"turn failed:\n{traceback.format_exc()}")
        try:
            return {"context": render(time.time(), None, False, False, None)}
        except Exception:
            return None
