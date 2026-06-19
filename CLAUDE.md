# CLAUDE.md — working notes for this repo

Family WhatsApp Assistant. WAHA delivers WhatsApp messages to a FastAPI app that
gates on an allow-list and replies via Claude. Claude has reminder tools; the
app schedules and proactively sends reminders.

## Conventions (honor these)

- **Secrets** live in `.env` only (gitignored). Never commit them.
- **Data** lives under `/data` (SQLite at `/data/app.sqlite`). The path is
  configurable via `DB_PATH`.
- **Timezones:** store `fire_at_utc` in UTC (naive datetimes that are always
  UTC); display in the member's local timezone. Conversions live in
  `app/brain.py` (`_local_iso_to_utc`, `_utc_to_local_str`).
- **Keep changes scoped.** Current slice is reminders only — no calendar, email,
  or automation framework yet.

## Layout

- `app/config.py` — settings + allow-list.
- `app/whatsapp.py` — WAHA client + webhook parsing.
- `app/db.py` — SQLAlchemy models (`FamilyMember`, `Reminder`, `MessageLog`),
  sessions, `get_or_create_member`.
- `app/scheduler.py` — APScheduler `AsyncIOScheduler` + persistent SQLAlchemy
  jobstore; `schedule_reminder`, `cancel_reminder_job`, `fire_reminder`.
- `app/brain.py` — Claude tool-use loop + tool implementations.
- `app/main.py` — FastAPI app, startup wiring, `/webhook`, `/health`.

## Scheduler invariants (do not break)

- Job id **is** the reminder id (str). Re-scheduling uses
  `replace_existing=True` → idempotent.
- `coalesce=True` + `misfire_grace_time=3600` → a reminder due during downtime
  fires **once** on restart, never multiple times, never silently dropped.
- The jobstore shares the app's SQLite DB so jobs persist and rehydrate on boot.
- `fire_reminder` runs **outside** the request: it opens its own DB session and
  makes its own WAHA call. Don't pass request-scoped state into it.

## Adding a Claude tool

1. Add the JSON schema to `TOOLS` in `app/brain.py`.
2. Implement `_tool_<name>(member, args) -> str` and register it in `_DISPATCH`.
3. Tools return a short string that goes back to Claude as a `tool_result`.

## Running / testing

See `README.md` ("Running" and "Acceptance tests"). Quick checks:
`curl localhost:8000/health` and `sqlite3 $DB_PATH "SELECT * FROM reminders;"`.
