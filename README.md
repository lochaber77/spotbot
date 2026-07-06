# Family WhatsApp Assistant

A self-hosted WhatsApp assistant for the family. WAHA + a FastAPI app that
receives messages, checks a sender allow-list, and replies via Claude. The brain
runs a **Claude tool-use loop**, and the first real feature — **reminders** — is
wired end to end: set/list/cancel in natural language, fired proactively when
due, and they **survive a restart**. See `CLAUDE.md` for state and
`family-whatsapp-assistant-spec.md` for the full plan.

## Prerequisites
- Docker + Docker Compose.
- An Anthropic API key.
- A **dedicated** WhatsApp number on a separate SIM (never your personal number).

## First run (local, on the dev PC)

1. Copy the env file and fill it in:
   ```
   cp .env.example .env
   ```
   Set `ANTHROPIC_API_KEY`, a `WAHA_API_KEY` secret, and put **only your own
   number** in `ALLOWED_NUMBERS` for first testing.

2. Bring up the stack (builds the app image locally):
   ```
   docker compose up --build
   ```

3. Pair the dedicated WhatsApp number:
   - Open the WAHA dashboard at `http://localhost:3000`.
   - Start the `default` session and **scan the QR code** with the dedicated
     number's WhatsApp (Linked Devices → Link a device).

4. Message the bot from your own number. It should reply.

## Moving to the Mac mini (cutover)
Don't ship a built image from the PC (x86-64) to the Mac (arm64). Move the
source and build on the Mac:

1. `git clone` the repo on the mini.
2. Copy over `.env` (and later the service-account JSON / Gmail tokens / SQLite DB).
3. `docker compose up --build` on the Mac (builds arm64).
4. Re-pair: start the WAHA session and **scan a fresh QR** on the mini. Unlink the
   dev device. Never run both hosts against the number at once.
5. Set the mini to never sleep; Docker `restart: unless-stopped` handles reboots/crashes.

## Endpoints
- `POST /webhook` — WAHA posts inbound messages here.
- `GET /health` — liveness check; reports the number of scheduled jobs.

## Reminders

Message the bot in natural language; Claude resolves relative times against the
family `TZ` and the app stores them in UTC:

- "remind me to take the bins out at 7am tomorrow"
- "what reminders do I have?"
- "cancel the bins reminder"

Reminders are stored in SQLite under `DATA_DIR` (`/data/app.sqlite`), and their
scheduled jobs live in the **same** file via APScheduler's jobstore — so a
pending reminder fires exactly once even across a restart.

### Acceptance tests

1. **Set:** "remind me to take the bins out at 7am tomorrow" → bot confirms with
   the parsed absolute local time; a `reminders` row (`status=scheduled`) and a
   scheduled job both exist.
2. **List:** "what reminders do I have?" → lists them in local time.
3. **Fire:** at the due time a WhatsApp reminder arrives (try "in 2 minutes" to
   verify quickly).
4. **Restart survival:** set one a few minutes out, `docker compose restart app`
   while it's pending, confirm it still fires exactly once.
5. **Cancel:** "cancel the bins reminder" → row marked `cancelled`, job removed.

Inspect state directly:

```
docker compose exec app python -c "import sqlite3;print(sqlite3.connect('/data/app.sqlite').execute('select id,text,fire_at_utc,status from reminders').fetchall())"
curl localhost:8000/health    # {"ok": true, "jobs": N}
```

## Shared calendar (optional)

The bot can read the family's **shared Google Calendar** and create events on it.
It authenticates as a **service account** (no per-person OAuth): create a calendar,
share it with the service account's email granting "Make changes to events", and
give the bot the calendar id + key file.

Enable it in `.env`:

```
GOOGLE_CALENDAR_ID=abc123@group.calendar.google.com
GOOGLE_SERVICE_ACCOUNT_HOST_PATH=./secrets/service-account.json   # host path to the key
```

then uncomment the read-only `/secrets/service-account.json` mount in
`docker-compose.yml`. Until both the id and a readable key are present, calendar
features stay off and the bot says so if asked.

Usage (natural language):
- "what's on this week?" → lists shared-calendar events in local time.
- "add football Saturday 10–11am" → the bot **proposes** it and asks you to
  confirm; it's created **only after you reply "yes"** (calendar events affect the
  whole family, so writes are confirm-first, with a `CONFIRMATION_TTL_MINUTES`
  expiry). Reminders remain low-stakes and execute directly.

## Email drafts (optional, per-member, opt-in)

The bot can draft email replies in a member's own Gmail — **drafts only, it never
sends**. Consumer Gmail can't use a service account, so this is per-user OAuth and
opt-in: only members who authorize get it.

Enable it for one member (run on a machine **with a browser**, not the container):

```
pip install google-auth-oauthlib
python scripts/gmail_authorize.py \
    --number 447700900000 \
    --client-secrets ~/client_secret.json \
    --out ./secrets/gmail
```

Set `GMAIL_TOKENS_HOST_PATH=./secrets/gmail` in `.env` and uncomment the read-only
`/secrets/gmail` mount in `docker-compose.yml`. A member is email-enabled only if a
`<number>.json` token exists; otherwise the bot says email isn't set up for them.

Usage (natural language): "reply to Nan saying we'll be there at 1" → the bot
**proposes** the draft and asks you to confirm; on "yes" it creates the draft in
your Gmail Drafts for you to review and send. Drafting reaches outside the system,
so it's confirm-first; **the code has no send path at all** (spec §9/§10).

## Safety notes
- Keep `ALLOWED_NUMBERS` tight; the bot ignores everyone else and all group chats.
- Never commit `.env` or any credentials. The service-account JSON and Gmail
  tokens live outside the repo and are mounted read-only.
- Email is **draft-only** — there is deliberately no send capability in the code.
