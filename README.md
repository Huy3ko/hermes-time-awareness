# hermes-time-awareness

A sense of time for Hermes agents — **without putting a timestamp on every message**.

Three dimensions, one injected block, and silence the rest of the time:

- **Now** — what time is it? Clock, date, weekday, day period. Every turn, system clock only.
- **Elapsed** — how long since we last spoke? Only when the gap actually matters.
- **Session** — how long has this conversation been running, and how much of that was work?

```text
[Temporal context]
Now: 2026-11-09 20:58:43 · Europe/Berlin · Monday · evening
Session: 5 hours so far · ~3 hours active · 55 turns
[/Temporal context]
```

A continuous conversation gets one line. A gap gets two. A long session gets three.

---

## Why "elapsed" and "active" are different numbers

Five hours between the first and last message is not five hours of work. It is
five hours of wall clock with pauses inside.

This plugin measures both and never conflates them:

| | meaning |
|---|---|
| **so far** | first user message → last user message |
| **~active** | sum of the gaps between consecutive user messages, **excluding any gap longer than `idle_break_seconds`** (default 10 minutes) |

The tilde is not decoration. Active time is **estimated from message spacing**,
not measured. An agent that researches for fifteen minutes without producing a
message shows that quarter hour as a pause. Read it as a lower bound, not as a
timesheet.

A conversation that runs 09:00–18:00 with two hours of breaks reports
**9 hours so far · 7 hours active**. An agent must never claim the former as the
latter.

---

## Install

**Current version: 2.0.1**

```bash
cd ~/.hermes/plugins
git clone https://github.com/Huy3ko/hermes-time-awareness.git
hermes plugins enable hermes-time-awareness
```

No restart needed — the gateway reloads the hook immediately and the next turn
carries the block.

Requirements: Hermes Agent with plugin support, Python 3.11+ (standard library only).

### Uninstall

```bash
hermes plugins disable hermes-time-awareness
rm -rf ~/.hermes/plugins/hermes-time-awareness
rm -f ~/.hermes/time-awareness.db
```

The database holds one row per conversation (`session_id`, `last_seen`) and no
message content. Deleting it loses only the cross-session history; the next turn
rebuilds it from `state.db` for the current session.

### Install without git

Download the release archive, unpack into `~/.hermes/plugins/hermes-time-awareness`,
then `hermes plugins enable hermes-time-awareness`. Verify against the SHA-256 in
the release notes before unpacking.

---

## Configuration

```yaml
plugins:
  enabled:
    - hermes-time-awareness
  entries:
    hermes-time-awareness:
      enabled: true
      min_gap_seconds: 7200        # 2 h before a gap note appears
      notify_date_change: true     # midnight rollover alone is enough
      exclude_platforms: [cron]    # background turns never touch the clock
      exclude_user_prefixes: []    # e.g. ["[background-event "]
      query_ttl_seconds: 60        # how often to re-read state.db
      idle_break_seconds: 600      # gap above this counts as a pause, not work
      debug: false
```

---

## How it decides

```text
pre_llm_call
  ├─ always: build the Now line from the system clock
  ├─ platform excluded (cron) or automated message?
  │     → clock only; no gap check, no write
  ├─ gap: own session (state.db) or newest row in the session table
  ├─ session: elapsed and active time, from user-message timestamps
  └─ record this conversation in the session table (throttled)
```

**Silence is the default.** Nothing is reported that does not carry information.

---

## The part that makes it fast

The obvious query is the wrong one:

```sql
SELECT timestamp FROM messages ORDER BY timestamp DESC LIMIT 1;
-- SCAN messages + TEMP B-TREE FOR ORDER BY → 33 s on a 4.9 GB database
```

There is no index on `timestamp` alone. Every index in `messages` starts with
`session_id`. On a database with 850k rows this query exceeds the 30-second hook
timeout — so it **never returns**, the hook is abandoned, no error is logged, and
the turn arrives without a time hint. It looks like everything works.

Three fixes:

The `LIMIT` applies **only to the gap lookup**. Session start, turn count and
active time come from their own aggregate query without a limit
(`MIN(timestamp), MAX(timestamp), COUNT(*)`), so a long conversation is measured
in full even when the lookback for the last exchange is bounded.

**1. Key the query by session.**

```sql
SELECT ... FROM messages
WHERE session_id = ? AND role IN ('user','assistant')
ORDER BY rowid DESC LIMIT 400
-- SEARCH messages USING INDEX idx_messages_session (session_id=?) → 1.4 ms warm
```

`rowid` is physically ordered, so no sort is needed. The `LIMIT` bounds a long
conversation with many tool calls: 649 rows took 345 ms on a cold page cache and
1.4 ms once warm. Cold only happens on the first turn after a gateway start.

**2. Keep a one-row-per-session table for cross-session gaps.**

```sql
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, last_seen REAL NOT NULL);
CREATE INDEX idx_sessions_last_seen ON sessions(last_seen DESC);
```

4,190 conversations occupy 428 KB; the lookup costs **0.05 ms**. The table grows
with the number of conversations, not with the number of messages.

**3. Backfill once, cheaply.** `GROUP BY session_id, MAX(timestamp)` fits an
existing covering index:

```sql
-- SCAN messages USING COVERING INDEX idx_messages_session_active → 368 ms
INSERT INTO sessions (session_id, last_seen)
SELECT session_id, MAX(timestamp) FROM messages GROUP BY session_id;
```

---

## What counts as "the last exchange"

The timestamp of the previous **completed** exchange, not the current one.

Walking backwards, a user row owns every assistant row up to the next user row,
tool calls included. The first assistant timestamp found after crossing a user
row is the moment the prior exchange finished — so typing a new message does not
reset the clock.

Automated turns are excluded: `display_kind: notification`, a
`notification_category` in `display_metadata`, or a configured user-message
prefix (also inside multimodal JSON blocks). A background reminder must not count
as a human conversation, and Cron turns never write to the session table.

For cross-session gaps, `last_seen` is the **turn** timestamp, not the assistant
reply — a deliberate simplification, marked as `other conversation` in the block.

---

## Privacy

- **No message content is written.** The only file is `time-awareness.db`: one
  row per conversation, two columns.
- `state.db` is opened `mode=ro` with a one-second timeout and reads the WAL
  tail, so a just-committed row is visible.
- The block goes into the **user turn**, never the system prompt — the prompt
  cache stays byte-stable.
- Every failure path returns no context instead of blocking a turn.

---

## Tests

```bash
python -m pytest tests -q
# 51 passed
```

Grouped by what they protect:

- **clock**: exact day-period boundaries for all 24 hours, coarse duration tiers,
  today/yesterday/N-days-ago labels, UTC fallback with an explicit label
- **history**: last completed exchange, newest assistant wins, an open user turn
  does not reset the clock, session isolation, uncheckpointed WAL visibility,
  old schemas without `display_kind`, notification rows ignored, multimodal
  prefix matching
- **store**: one row per session, throttled writes, fast lookup with 5,000
  sessions, broken-store resilience
- **activity**: elapsed versus active, pauses above the threshold excluded,
  single-message sessions, custom break, caching
- **hook**: clock always present, short gap silent, cron gets the clock but no
  gap and no write, cross-session marking, midnight rollover, fail-open

The WAL test is the one that matters most:

> A committed but not yet checkpointed assistant row must be visible over a
> read-only connection — otherwise a resume right after the prior turn misses the
> gap, and the plugin silently does nothing exactly when it is needed.

---

## Provenance

The *idea* — silence by default, a gap note only when it carries information, and
a session-keyed query instead of a table scan — comes from
[time-gap](https://github.com/Randool/time-gap) by Randool.

This repository is an independent implementation: different module layout,
different function boundaries, a clock dimension, session-activity measurement,
and its own test suite written from the behaviour rather than from the original.
It shares the design, not the code.

## Licence

MIT — see [LICENSE](LICENSE).

**Provenance.** The design follows time-gap, which carries no licence file. This
repository is an independent implementation — different module layout, different
function boundaries, and a test suite written from the behaviour. It shares the
design, not the code. Every function and every test here was written for this
project; see `CHANGELOG.md` §2.0.0 for the replacement that made that true.
