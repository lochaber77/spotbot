# CLAUDE.md — context for Claude Code

## What this is
A self-hosted WhatsApp assistant for a family. It organizes/plans, sends
reminders, drafts email replies (draft-only, never auto-send), and runs a small
allow-list of pre-agreed automations with confirm-first defaults. Practical
household tool, not production-grade. Full spec: `family-whatsapp-assistant-spec.md`.

## Current state
**M1 plumbing + first vertical slice: reminders.** WAHA + a FastAPI app that
receives inbound messages, enforces a sender allow-list, and replies via Claude.
The brain is now a **tool-use loop** (M2 brought forward) and reminders are wired
end to end (the scheduler half of M3 was pulled forward, since reminders are the
core feature and exercise the whole stack): a member can set/list/cancel
reminders in natural language, and the app proactively sends each one when due
and survives a restart. **Shared Google Calendar is now wired too** (rest of M3):
read the calendar and create events — writes are **confirm-first** (propose →
"yes" → create). Calendar features stay disabled until a service account +
calendar id are configured. **Gmail draft replies are wired too (M4)**:
per-member OAuth, opt-in, and **draft-only — the bot never sends**. Email stays
off for a member until they've authorized (a token file exists). The **M5
automation framework** is in place too: an allow-list of declared automations
(`propose_automation`), consequential ones routed through the same confirm-first
mechanism, with consent recorded on first run. One worked example ships
(`broadcast_reminder`); real automations are added to the registry as needed.

### Code layout (flat modules in `app/`, built via `docker compose build ./app`)
- `app/config.py` — env settings + allow-list; `DATA_DIR`/`DB_PATH`/`DB_URL`;
  `CALENDAR_ENABLED`, `CONFIRMATION_TTL_MINUTES`.
- `app/whatsapp.py` — WAHA client (`send_text(number, text)`).
- `app/main.py` — FastAPI `/webhook` + `/health`; lifespan inits DB + scheduler.
- `app/brain.py` — Claude tool-use loop; reminder + calendar tools + UTC↔local
  helpers (`_local_iso_to_utc`, `_utc_to_local_str`); confirm-first via
  `_pending_for` + `resolve_confirmation`.
- `app/gcal.py` — Google Calendar client (service account); `create_event`,
  `list_events`. Google libs imported lazily so it's disabled-safe.
- `app/gmail.py` — Gmail client, **draft-only** (`create_draft`, `has_credentials`);
  per-member OAuth tokens under `GMAIL_TOKENS_DIR`. No send path exists (spec §9/§10).
- `scripts/gmail_authorize.py` — one-time local OAuth helper that writes a
  member's token file (run on a machine with a browser, not the container).
- `app/automations.py` — M5 allow-list registry (`AutomationSpec`), confirm-first
  routing, consent recording, `sync_db`. Add automations here.
- `app/db.py` — SQLAlchemy models (`FamilyMember`, `Reminder`, `Message`,
  `CalendarEvent`, `PendingConfirmation`, `EmailDraft`, `Automation`), sessions,
  `get_or_create_member`.
- `app/scheduler.py` — APScheduler `AsyncIOScheduler` + persistent SQLAlchemy
  jobstore; `schedule_reminder`, `cancel_reminder_job`, `fire_reminder`.

## Stack
- WhatsApp: **WAHA** (Docker) — unofficial library wrapper. (Alternative: Baileys-direct.)
- App: **Python 3.12 + FastAPI**, Anthropic SDK (Claude tool-use planned in M2).
- Datastore: **SQLite** to start (file-copy migration); Postgres is the upgrade path.
- Scheduler (M3): APScheduler with a **persistent jobstore** — reminders must survive restarts.
- Calendar (M3): one **shared Google Calendar** via a **service account** (no per-person OAuth).
- Email (M4): Gmail **drafts** endpoint, **per-user OAuth**, opt-in. Never sends.
- Packaging: Docker + docker-compose; **build from source on the Mac** (arm64).

## Milestones
1. Plumbing — echo + allow-list (DONE).
2. Brain — Claude tool-use loop turning messages into structured intents (DONE).
3. Calendar + reminders — **DONE**: reminders (fire + survive restart) and
   shared-calendar read/write (writes are confirm-first).
4. Email drafts — per-user Gmail OAuth, draft-on-request. **DONE** (draft-only,
   confirm-first, opt-in per member).
5. Automations — allow-list + confirm-first + consent recording. **Framework
   DONE** (one example automation; add real ones to `app/automations.py`).
6. Cutover — move to the Mac mini.

## Scheduler invariants (do not break)
- Job id **is** the reminder id (str). Re-scheduling uses `replace_existing=True`
  → idempotent.
- `coalesce=True` + `misfire_grace_time=3600` → a reminder due during downtime
  fires **once** on restart, never multiple times, never silently dropped.
- The jobstore shares the app's SQLite DB so jobs persist and rehydrate on boot.
- `fire_reminder` runs **outside** the request: its own DB session, its own WAHA
  call. Don't pass request-scoped state into it.

## Adding a Claude tool
1. Add the JSON schema to `TOOLS` in `app/brain.py`.
2. Implement `_tool_<name>(member, args) -> str` and register it in `_DISPATCH`.
3. Tools return a short string that goes back to Claude as a `tool_result`.

## Adding an automation (M5)
1. Append an `AutomationSpec` to `AUTOMATIONS` in `app/automations.py` with a
   clear `usage` string and an `execute(member, args) -> str`.
2. Set `requires_confirmation=True` for anything that affects others / reaches
   outside the system (routes through confirm-first); `False` only for
   low-stakes, self-affecting actions.
3. It's exposed to Claude via `propose_automation` and listed in the system
   prompt automatically; `sync_db()` upserts its row on startup.

## Conventions / guardrails
- Secrets in `.env` (gitignored). Service-account JSON and OAuth tokens live
  outside the repo, mounted read-only. Never commit secrets.
- Dedicated WhatsApp number on a separate SIM — never a personal number.
- Re-pair WhatsApp with a fresh QR on each host; never run two hosts against the
  number at once.
- Email is **draft-only**. No send capability should exist in code.
- Consequential actions (shared events, emails) go through confirm-first.
  Personal reminders are low-stakes and execute directly (no confirm).
  Mechanism: `create_calendar_event` / `draft_email` write a `PendingConfirmation`
  row (kind `calendar_event` / `email_draft`) instead of acting; the brain injects
  the latest un-expired pending row into the system prompt, and
  `resolve_confirmation(id, approve)` executes or declines it by kind. This
  works across messages with no conversation memory, and expires after
  `CONFIRMATION_TTL_MINUTES`.
- **Timezones:** store `fire_at_utc` in UTC (naive datetimes that are always
  UTC); display in the member's local timezone. Conversions live in `app/brain.py`.

## Run
See `README.md`. Inbound webhook is `POST /webhook`; health is `GET /health`.

## Open decisions (pick as you go)
- WAHA+Python (current) vs Baileys-direct.
- SQLite (current) vs Postgres.
- Which Google account owns the shared calendar.
- Single family timezone vs per-member overrides. (Currently: members carry a
  `timezone` column defaulting to the family `TZ` — single-tz today, per-member
  later without a migration.)
- Confirmation-expiry window length.
