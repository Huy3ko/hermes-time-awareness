# hermes-time-awareness

A Hermes plugin that gives the model a sense of time — **without putting a timestamp on every message**.

Two dimensions, one injected block:

- **Now** — what time is it? Clock, date, weekday, day period. Every turn, no I/O.
- **Elapsed** — how long has it been? Only when the gap actually matters.

```text
[Temporal context]
Now: 2026-11-09 20:22:07 · Europe/Berlin · Monday · evening
Last exchange: 2026-11-08 22:22 · 22 hours ago · yesterday · other conversation
[/Temporal context]
```

That is the whole output. Two lines on a normal turn, three when a gap is worth mentioning.

---

## Why two dimensions

A clock alone is not a sense of time. It prevents "good morning" at 19:45, but it does not notice that a plan from yesterday is stale, that a deadline moved overnight, or that this conversation resumed after three days.

And a gap alone is not enough either: the model still needs to know what time it is right now.

Keeping them separate matters because they have very different costs:

| | Source | Cost |
|---|---|---|
| **Now** | system clock | ~13 µs, no I/O |
| **Elapsed** | `state.db`, read-only | ~16 ms first read, then cached |
| **Elapsed, other sessions** | separate one-row-per-session table | ~0.05 ms |

---

## Install

```bash
cd ~/.hermes/plugins
git clone https://github.com/Huy3ko/hermes-time-awareness.git
hermes plugins enable hermes-time-awareness
```

No restart required — the hook is picked up on the next turn.

Requirements: Hermes Agent with plugin support, Python 3.11+ (standard library only — `zoneinfo`, `sqlite3`).

---

## Configuration

All keys are optional. Defaults are deliberately conservative.

```yaml
plugins:
  enabled:
    - hermes-time-awareness
  entries:
    hermes-time-awareness:
      enabled: true
      min_gap_seconds: 7200        # 2 hours before a gap note appears
      notify_date_change: true     # midnight rollover alone is enough
      exclude_platforms: [cron]    # background turns never touch the clock
      exclude_user_prefixes: []    # e.g. ["[background-event "]
      query_ttl_seconds: 60        # how often to re-read the session
      debug: false                 # write decisions to debug.log
```

---

## How it decides

```text
pre_llm_call
  ├─ always: build the Now line from the system clock
  ├─ platform excluded (cron) or notification turn?
  │     → no gap check, no write
  ├─ read the last completed exchange
  │     ├─ own session: last assistant row in state.db (indexed, cached)
  │     └─ no own history yet: newest row in the session table
  ├─ gap ≥ min_gap_seconds OR calendar day changed?
  │     → add the Last exchange line
  └─ record this session's timestamp (throttled)
```

**Silence is the default.** A continuous conversation produces the Now line and nothing else.

---

## The part that makes it fast

The obvious query is the wrong one:

```sql
SELECT timestamp FROM messages ORDER BY timestamp DESC LIMIT 1;
-- SCAN messages + TEMP B-TREE FOR ORDER BY → 33 s on a 4.9 GB database
```

There is no index on `timestamp` alone. All indexes in `messages` start with `session_id`. On a database with 850k rows, that query runs longer than the 30-second hook timeout — so it **never returns a result**, the hook is abandoned, no error is logged, and the turn simply arrives without a time hint. It looks like everything is working.

Two things fix it:

**1. Key the query by session.**

```sql
SELECT ... FROM messages
WHERE session_id = ? AND role IN ('user','assistant')
ORDER BY rowid DESC
-- SEARCH messages USING INDEX idx_messages_session (session_id=?) → ~16 ms
```

`rowid` is physically ordered, so no sort is needed, and the loop stops as soon as the previous exchange is found.

**2. Keep a one-row-per-session table for cross-session gaps.**

For "when did we last speak at all?", scanning `messages` is hopeless. Instead:

```sql
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, last_seen REAL NOT NULL);
CREATE INDEX idx_sessions_last_seen ON sessions(last_seen DESC);
```

The table grows with the number of conversations, not the number of messages. 4,190 sessions occupy 428 KB, and the lookup costs **0.05 ms**.

A one-time backfill is cheap because `GROUP BY session_id, MAX(timestamp)` fits an existing covering index:

```sql
-- SCAN messages USING COVERING INDEX idx_messages_session_active → 368 ms for 4,190 sessions
INSERT INTO sessions (session_id, last_seen)
SELECT session_id, MAX(timestamp) FROM messages GROUP BY session_id;
```

---

## What "last exchange" means

The timestamp of the **previous completed exchange**, not the current one.

Walking backwards, a user row owns every assistant row up to the next user row, tool calls included. The first assistant timestamp found *after* crossing a user row is the moment the prior exchange finished. That naturally excludes the current user turn, so typing a new message does not reset the clock.

Notification turns are excluded: `display_kind: notification`, a `notification_category` in `display_metadata`, or a configured user-message prefix. A background reminder must not count as a human conversation.

For **cross-session** gaps, `last_seen` is the turn timestamp, not the assistant reply — a deliberate simplification. Cron and notification turns never write to the table, so automation cannot shift the human conversation clock.

---

## Privacy

- The plugin **writes nothing** to your messages, sessions or memory. The only file it creates is its own `time-awareness.db` (one row per session, no message content).
- `state.db` is opened `mode=ro` with a 1-second timeout; it reads the WAL tail, so a just-committed row is visible.
- The injected block lives in the **user turn**, never the system prompt — the prompt cache stays byte-stable.
- Every failure path returns no context instead of blocking a turn.

---

## Tests

```bash
python -m pytest tests -q
# 41 passed
```

Coverage includes exact day-period boundaries, gap tiers, the query cache, platform exclusion, notification markers, midnight rollover in both directions, uncheckpointed WAL visibility, session isolation, one-row-per-session growth, and fail-open behaviour on a broken database.

The WAL test is the important one:

> A committed but not yet checkpointed assistant row must still be visible over a read-only connection — otherwise a resume right after the prior turn misses the gap, and the plugin silently does nothing exactly when it is needed.

---

## Credits and provenance

The two-dimension design, the session-keyed query shape, the notification filtering and the midnight-rollover rule follow **time-gap** by [Randool](https://github.com/Randool/time-gap), which introduced the "silence is the default" approach for Hermes.

This implementation is a rewrite that adds the clock dimension, merges both into a single injection, and replaces the cross-session path with a dedicated table. The original repository carries no licence file; this one is MIT for the code contained here.

## Licence

MIT — see [LICENSE](LICENSE).
