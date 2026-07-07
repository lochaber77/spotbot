# DEPLOY.md — running the Family WhatsApp Assistant

A single runbook: from an empty Mac mini to a live, always-on bot, then how to
switch on calendar and email. Do the phases in order; each one is safe to stop
at. `README.md` has the feature-level detail this summarizes.

> **Golden rules (read once):**
> - Use a **dedicated WhatsApp number on a separate SIM** — never a personal number.
> - **Pair the number on exactly one host at a time.** Two hosts against one
>   number will fight over the session and disconnect. Moving hosts = fresh QR +
>   unlink the old device.
> - **Never commit secrets.** `.env`, the Google service-account JSON, and Gmail
>   tokens are all gitignored / mounted read-only.
> - **The bot never sends email** — drafts only. There is no send path in the code.

---

## Phase 0 — Prerequisites (on the Mac mini)

- **Docker** with Compose: Docker Desktop, or `colima` + `docker` CLI.
- **Git**, and this repo cloned:
  ```bash
  git clone <your-repo-url> spotbot && cd spotbot
  ```
- An **Anthropic API key**.
- The **dedicated WhatsApp number** active on a phone (to scan the pairing QR).

The mini is arm64; everything builds natively there. Don't ship a built image
from another machine — move the source and `docker compose up --build` here.

---

## Phase 1 — First run (smoke test, your number only)

Goal: prove the whole stack end to end before adding family. Calendar and email
stay off (blank creds) — that's expected.

1. Create your env file:
   ```bash
   cp .env.example .env
   ```
2. Edit `.env` and set:
   - `ANTHROPIC_API_KEY=…`
   - `WAHA_API_KEY=` — pick any secret string (protects the WAHA API).
   - `ALLOWED_NUMBERS=` — **only your own number**, international format, no `+`
     (e.g. `447700900000`).
   - `TZ=` — your family timezone (e.g. `Europe/London`).
   - Leave `GOOGLE_*` and `GMAIL_*` blank for now.
3. Build and start:
   ```bash
   docker compose up --build
   ```
4. Pair WhatsApp:
   - Open the WAHA dashboard: <http://localhost:3000>.
   - Start the `default` session and **scan the QR** with the dedicated number's
     WhatsApp (Linked Devices → Link a device).
5. Verify (from your own phone, messaging the dedicated number):
   - "hi" → a friendly reply.
   - "remind me to test the bot in 2 minutes" → confirmation with the absolute
     time; ~2 min later the reminder arrives.
   - "what reminders do I have?" → lists it.
   - "cancel that reminder" → confirms cancelled.
6. Health check:
   ```bash
   curl localhost:8000/health          # {"ok": true, "jobs": N}
   ```

**Restart-survival check (do this once):** set a reminder a few minutes out, then
`docker compose restart app` while it's pending. It should still fire exactly
once. This is the core guarantee — worth confirming on the real host.

Only after this passes, widen `ALLOWED_NUMBERS` to the rest of the family
(comma-separated) and `docker compose up -d` to restart.

---

## Phase 2 — Enable the shared calendar (optional)

Consumer Calendar uses a **service account** (no per-person OAuth).

1. In Google Cloud: create a project, enable the **Google Calendar API**, create
   a **service account**, and download its **JSON key**.
2. Create/choose the family Google Calendar. In its settings, **share it with the
   service account's email**, granting **"Make changes to events."** Copy the
   calendar id (looks like `…@group.calendar.google.com`).
3. Put the key on the mini **outside** the repo tree you commit from, e.g.:
   ```bash
   mkdir -p ./secrets && mv ~/Downloads/service-account.json ./secrets/
   ```
4. In `.env`:
   ```
   GOOGLE_CALENDAR_ID=…@group.calendar.google.com
   GOOGLE_SERVICE_ACCOUNT_HOST_PATH=./secrets/service-account.json
   ```
5. In `docker-compose.yml`, **uncomment** the read-only service-account mount
   under the `app` service (the `…:/secrets/service-account.json:ro` line).
6. `docker compose up -d --build`, then test from WhatsApp:
   - "what's on this week?" → lists shared-calendar events.
   - "add football Saturday 10–11am" → the bot **proposes** it and asks you to
     confirm; reply "yes" → it's created (calendar writes are confirm-first).

Until both the id and a readable key are present, the bot just says the calendar
isn't configured — no errors.

---

## Phase 3 — Enable Gmail drafts (optional, per member)

Consumer Gmail needs **per-user OAuth**, and it's **draft-only** — the bot writes
a draft into that person's Gmail for them to review and send. Repeat per member
who wants it.

1. In Google Cloud: enable the **Gmail API** and create an **OAuth client** of
   type **Desktop app**; download its `client_secret.json`.
2. On a machine **with a browser**, run the one-time consent helper (this is the
   step that can't be done on a headless server):
   ```bash
   pip install google-auth-oauthlib
   python scripts/gmail_authorize.py \
       --number 447700900000 \
       --client-secrets ~/client_secret.json \
       --out ./secrets/gmail
   ```
   This writes `./secrets/gmail/447700900000.json`.
3. Copy the `./secrets/gmail/` directory to the mini (outside the committed
   tree). In `.env`:
   ```
   GMAIL_TOKENS_HOST_PATH=./secrets/gmail
   ```
4. In `docker-compose.yml`, **uncomment** the read-only `…:/secrets/gmail:ro`
   mount under the `app` service.
5. `docker compose up -d --build`, then test from that member's WhatsApp:
   - "draft a reply to nan@example.com saying we'll be there at 1" → the bot
     **proposes** the draft and asks to confirm; on "yes" it's created in that
     member's Gmail Drafts. **It never sends.**

A member with no token file simply isn't offered email.

---

## Phase 4 — Real automations (optional, ongoing)

The framework ships one example, `broadcast_reminder`. Add your own by editing
`app/automations.py` (append an `AutomationSpec`; see `CLAUDE.md` → "Adding an
automation"), then rebuild. Consequential ones (affecting others / reaching
outside) should keep `requires_confirmation=True`.

---

## Always-on operations (the mini as the live host)

- **Disable system sleep** (System Settings → Energy Saver / `pmset`). A sleeping
  mini means missed messages and dead reminders.
- **Start on boot:** Docker Desktop "start at login", or run the stack under
  `launchd`. Compose already uses `restart: unless-stopped`, so it recovers from
  crashes and reboots.
- **Backups:** the SQLite DB (reminders + jobs) lives in the `app-data` Docker
  volume. To snapshot it:
  ```bash
  docker compose cp app:/data/app.sqlite ./backup-app.sqlite
  ```
- **Logs:** `docker compose logs -f app`.

---

## Verification checklist

- [ ] `curl localhost:8000/health` returns `{"ok": true, …}`.
- [ ] Reply received for an allow-listed sender; ignored for a non-listed one.
- [ ] A near-future reminder fires; survives `docker compose restart app` once.
- [ ] (If calendar on) "what's on?" reads; "add …" proposes → confirm → created.
- [ ] (If email on) "draft …" proposes → confirm → draft appears in Gmail; nothing
      is ever sent.
- [ ] Mini won't sleep; stack restarts on reboot.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| No reply at all | Sender not in `ALLOWED_NUMBERS` (digits, no `+`); or WAHA session not paired — re-scan the QR at `:3000`. |
| Reply, but reminders never fire | Mini is sleeping; or the container was recreated without the `app-data` volume (jobs live there). |
| "calendar isn't configured" | `GOOGLE_CALENDAR_ID` blank or key file not readable at the mount path. |
| "email isn't set up for you" | No `<number>.json` under `GMAIL_TOKENS_DIR`; run the authorize helper and mount the dir. |
| Session keeps dropping | Two hosts are paired to the same number — unlink all but one, re-pair with a fresh QR. |
| WAHA calls rejected | `WAHA_API_KEY` mismatch between the `waha` and `app` services (both read the same `.env`). |
