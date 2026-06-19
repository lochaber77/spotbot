# Family WhatsApp Assistant (spotbot)

A WhatsApp assistant for a family. Messages arrive via [WAHA](https://waha.devlike.pro/)
(WhatsApp HTTP API) into a FastAPI app, which enforces a sender allow-list and
replies via Claude. Claude has tools to manage **reminders** — set, list, and
cancel — and the app proactively sends each reminder when it's due, surviving
restarts.

## Status

- **Milestone 1 (done):** WAHA + FastAPI receive messages, allow-list gate,
  single-turn Claude reply.
- **Milestone 2 (this slice):** reminders end-to-end — Claude tool-use →
  SQLite datastore → APScheduler → outbound WhatsApp.

## Architecture

```
WhatsApp ──► WAHA ──► POST /webhook (FastAPI)
                          │
                          ├─ allow-list gate
                          ├─ resolve sender → family_member
                          └─ brain.handle_message()  ── Claude tool-use loop
                                   │   set_reminder / list_reminders / cancel_reminder
                                   ▼
                          db.py (SQLite @ /data/app.sqlite, SQLAlchemy)
                                   ▲
                          scheduler.py (APScheduler AsyncIOScheduler)
                                   │  persistent SQLAlchemy jobstore (same DB)
                                   ▼
                          fire_reminder() ──► whatsapp.send_text() ──► WAHA ──► WhatsApp
```

| File | Responsibility |
|------|----------------|
| `app/config.py` | Env/`.env` settings, number normalisation, allow-list. |
| `app/whatsapp.py` | WAHA client (`send_text`), inbound webhook parsing. |
| `app/db.py` | SQLAlchemy models + sessions; member lookup/seed. |
| `app/scheduler.py` | APScheduler + persistent jobstore; `schedule_reminder`, fire callback. |
| `app/brain.py` | Claude tool-use loop + reminder tool implementations. |
| `app/main.py` | FastAPI app, startup wiring, webhook handler. |

### Why the scheduler matters

Jobs are stored in a SQLAlchemy jobstore that points at the **same SQLite file**
as the app data, so they're rehydrated on boot. Each reminder's job id **is** the
reminder id, scheduling uses `replace_existing=True` (idempotent), and jobs run
with `coalesce=True` and a 1-hour `misfire_grace_time`. Net effect: a reminder
whose time passed while the app was down still fires **exactly once** on restart,
and cancelling removes the exact job.

## Configuration

Copy `.env.example` to `.env` and fill it in. Secrets stay in `.env` (gitignored);
data lives under `/data`.

Key vars: `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `WAHA_BASE_URL`, `WAHA_SESSION`,
`ALLOWED_NUMBERS` (comma-separated), `DB_PATH`, `DEFAULT_TIMEZONE`.

## Running

### Docker Compose (WAHA + app together)

```bash
cp .env.example .env   # then edit it
docker compose up --build
```

Then open WAHA at http://localhost:3000, start the `default` session, and scan
the QR with WhatsApp. WAHA is pre-configured to POST message events to the app's
`/webhook`.

### Locally (app only, point at an existing WAHA)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt
export DB_PATH=./data/app.sqlite   # or set in .env
uvicorn app.main:app --reload
```

## Acceptance tests

With a WhatsApp number on the allow-list, message the bot:

1. **Set:** "remind me to take the bins out at 7am tomorrow" → bot confirms with
   the parsed absolute local time. A `reminders` row (`status=scheduled`) and an
   APScheduler job exist.
2. **List:** "what reminders do I have?" → it lists them with local times.
3. **Fire:** at the due time a WhatsApp reminder arrives (try a near-future time
   like "in 2 minutes" to verify quickly).
4. **Restart survival:** set a reminder a few minutes out, restart the app
   (`docker compose restart app`), and confirm it still fires exactly once.
5. **Cancel:** "cancel the bins reminder" → the row is marked `cancelled` and the
   job is removed.

Inspect state directly:

```bash
sqlite3 ./data/app.sqlite \
  "SELECT id,text,fire_at_utc,status FROM reminders; SELECT id FROM apscheduler_jobs;"
curl localhost:8000/health   # {"status":"ok","jobs":N}
```

> Timezones: `fire_at_utc` is stored in UTC; the bot displays times in the
> member's local timezone.
