# Family WhatsApp Assistant — Project Spec

> Working title. A self-hosted assistant that lives on WhatsApp and helps a family
> organize, schedule, and get reminded about things, draft email replies on request,
> and run a small set of pre-agreed automations. Built as a practical household tool,
> not a production-grade service.

---

## 1. Purpose & non-goals

**Purpose.** Give the family one WhatsApp contact that can:
- Understand messages in natural language and keep useful context.
- Add things to a shared family calendar and answer "what's on" questions.
- Send proactive reminders at the right time.
- Draft email replies on request (never send them automatically).
- Run a small allow-list of automations, with confirmation for anything consequential.

**Non-goals.**
- Not a customer-facing or commercial bot. Family use only, low volume.
- No auto-sending of email — ever. Drafts only.
- Not aiming for high availability or horizontal scale. One always-on machine is enough.
- Not using the official WhatsApp Business Platform (see §3 for the deliberate trade-off).

---

## 2. Users & scale

- A handful of family members, each identified by their WhatsApp number.
- Only numbers on an explicit allow-list may interact with the bot; everyone else is ignored.
- Volume is a few dozen messages a day at most. Design for simplicity, not throughput.
- One shared timezone by default, with an optional per-member override.

---

## 3. The WhatsApp layer (and the deliberate trade-off)

The assistant connects to WhatsApp through an **unofficial library** rather than the official
Business API. This is a conscious choice: the official API forces all proactive messages
(reminders) through rigid pre-approved templates, which kills the natural, contextual nudging
that makes this assistant worth building.

**Accepted risk.** Unofficial automation violates WhatsApp's Terms of Service and carries a
ban risk. That risk is driven by spam-like behavior (bulk sends, messaging strangers), none of
which applies to a low-volume family tool. Mitigations:
- Runs on a **dedicated WhatsApp number on a separate SIM** — never anyone's personal number,
  so a worst-case ban can't take down a real account.
- Strict sender allow-list; the bot never messages anyone who hasn't opted in.
- Low volume, human-like cadence, no bulk broadcasting.

**Recommended implementation: WAHA** (`devlikeapro/waha`) — a Dockerized HTTP wrapper around the
unofficial protocol that exposes a simple webhook (inbound) and REST endpoint (send). This keeps
the messy session-management concern in its own container and lets the main app be written in any
language.

- **Alternative:** use **Baileys** (Node/TypeScript) directly, if you'd rather have a single-language
  Node codebase and own the session handling yourself. Pick one; see §15 Open Decisions.

**Session pairing.** The library links to the dedicated number as a *linked device* via QR scan.
The session is stored on disk. Re-pair with a fresh QR scan on the target machine rather than copying
session files; **never run two machines against the same number simultaneously** — they will fight over
the session and disconnect.

---

## 4. Architecture

```
WhatsApp (dedicated number)
        │  inbound webhook / outbound REST
        ▼
   ┌──────────┐      ┌─────────────────────────────┐
   │   WAHA   │◄────►│   App service (the "brain")  │
   │ (Docker) │      │  - FastAPI webhook receiver  │
   └──────────┘      │  - Claude tool-use loop      │
                     │  - Scheduler (APScheduler)   │
                     │  - Google Calendar client    │
                     │  - Gmail draft client        │
                     │  - Automation engine         │
                     └──────────────┬──────────────┘
                                    │
                          ┌─────────▼─────────┐
                          │   Datastore        │
                          │  (SQLite to start) │
                          └────────────────────┘
        external: Anthropic API · Google Calendar API · Gmail API
```

All services run under one `docker-compose` stack on the always-on machine.

---

## 5. Tech stack (concrete starting points)

| Concern | Choice | Notes |
|---|---|---|
| WhatsApp layer | WAHA (Docker) | Baileys-direct is the single-language alternative |
| App language | Python 3.12 | Strong LLM + Google API ecosystem |
| Web framework | FastAPI | Receives the WAHA webhook |
| LLM / brain | Anthropic SDK, Claude with tool use | Intent, planning, drafting — no hand-rolled NLU |
| Scheduler | APScheduler w/ persistent jobstore | Jobs must survive restarts |
| Datastore | SQLite to start | Migration = copy one file; Postgres is the upgrade path |
| Calendar | `google-api-python-client` | Service account → shared calendar (see §8) |
| Email | Gmail API, drafts endpoint | Per-user OAuth, opt-in, later phase (see §9) |
| Packaging | Docker + docker-compose | Build from source on the target machine |
| Process mgmt | compose `restart: unless-stopped` | Plus OS-level auto-start |

These are defaults to start from, not constraints to defend.

---

## 6. Data model

Tables (SQLite to start; names indicative):

- **family_members** — `id, name, whatsapp_number, role (parent/child), timezone, preferences (json), active`
- **messages** — `id, member_id, direction (in/out), body, ts` — rolling log for context
- **items** — `id, title, notes, created_by, status, due_at, source` — tasks / things to track
- **reminders** — `id, item_id (nullable), member_id, message_text, fire_at, recurrence (nullable), status, sent_at`
- **calendar_events** — `id, google_event_id, title, start, end, attendees, created_by` — cache of events the bot created
- **automations** — `id, name, description, trigger, action, requires_confirmation (bool), enabled, consent_recorded_at`
- **pending_confirmations** — `id, automation_id, payload (json), status (pending/confirmed/declined/executed), created_at, expires_at`
- **email_drafts** — `id, member_id, gmail_draft_id, subject, body, in_reply_to, status`

---

## 7. The brain — message handling loop

1. Inbound message hits the FastAPI webhook from WAHA.
2. Reject immediately if the sender isn't on the allow-list.
3. Load the member, their preferences, and recent message context.
4. Call Claude with a system prompt + the available **tools** (see below). Claude decides intent;
   we do not maintain a separate classifier.
5. Execute any tool calls — gated by the confirmation rules in §10.
6. Send Claude's reply back through WAHA.
7. Log inbound + outbound to `messages`.

**Tools exposed to Claude (function/tool-use):**
- `create_calendar_event(title, start, end, attendees?)`
- `list_schedule(member?, date_range)`
- `set_reminder(member, text, fire_at, recurrence?)`
- `list_reminders(member?)`
- `cancel_reminder(reminder_id)`
- `draft_email(member, to, subject, body, in_reply_to?)`
- `propose_automation(name, payload)` — routes through the confirm-first flow
- `add_item` / `list_items` / `complete_item`

---

## 8. Google Calendar integration (shared calendar)

The family uses **one shared calendar** that everyone subscribes to — no per-person OAuth.

- Create the family calendar under a regular Google account.
- Create a **Google service account**; share the family calendar with the service account's email,
  granting "Make changes to events."
- The bot authenticates as the service account — no interactive OAuth, no token-refresh expiry to babysit.
- Family members subscribe to the same calendar in their own Google Calendar app as normal.
- Store the service account JSON **outside** the repo; mount it read-only into the container.

This gives the bot exactly one set of calendar credentials and keeps the subscribe-side trivial for everyone.

---

## 9. Gmail draft integration (later phase, opt-in)

Asymmetry to note: unlike Calendar, consumer Gmail can't be accessed by a service account, so email
**requires per-user OAuth**.

- Only wired up for whoever actually wants email-drafting; everyone else never grants it.
- Uses the Gmail **drafts** endpoint: the bot writes the reply and leaves it in that person's Drafts
  for a one-tap human send. **It never sends.**
- Store each user's OAuth refresh token encrypted/at minimum gitignored; one token per opted-in member.
- Defer to a milestone after calendar + reminders are solid (§13).

---

## 10. Automations & the confirmation model

Every automation is a declared entry in `automations` with a `requires_confirmation` default of **true**
for anything consequential.

- **Auto (no confirmation):** low-stakes, self-affecting actions — set a personal reminder, answer a
  schedule query, add an item.
- **Confirm-first:** anything that affects others or reaches outside the system — creating/deleting shared
  events, proposing an email. The bot states the action and waits for an explicit "yes" (with an expiry
  window via `pending_confirmations`) before executing, then records consent.
- **Hard rule:** email is **draft-only**. Even a confirmed "send the email" action only ever creates a
  Gmail draft.

---

## 11. Reminders & scheduler

- When something is scheduled, write a `reminders` row **and** register an APScheduler job for `fire_at`.
- On fire: compose the message, send via WAHA to the member's number, mark `sent_at`.
- Support recurring reminders via APScheduler cron/interval triggers.
- **Critical:** use a persistent jobstore and **rehydrate jobs on startup** so reminders survive restarts
  and reboots. A reminder that silently vanishes after a crash is the worst failure mode here.
- The host must not sleep (see §12) or scheduled reminders won't fire.

---

## 12. Deployment: Windows dev → Mac mini (Apple Silicon)

Development happens on a **Windows PC**; the always-on host is a **Mac mini (Apple Silicon / arm64)**.
The two differ in OS *and* CPU architecture, so the setup is built to make that a non-event.

**Portability rules (from day one):**
- Code in **git**. Secrets in a gitignored **`.env`**; service-account JSON and OAuth tokens never committed.
- Everything runs in **Docker** so the runtime is identical across Windows and macOS.

**Architecture note:** don't ship a *built* image from the x86-64 PC to the arm64 Mac. Move the **source +
Dockerfile** and run `docker compose up --build` **on the Mac**, which builds natively for arm64. Common base
images (Python, Node, WAHA) are multi-arch, so this just works.

**Cutover checklist (PC → Mac mini):**
1. `git clone` the repo on the Mac mini.
2. Copy over `.env`, the Google service-account JSON, any Gmail OAuth tokens, and the SQLite DB file (if migrating data).
3. `docker compose up --build` on the Mac (builds arm64).
4. Start WAHA, scan a **fresh QR** to pair the dedicated number, and unlink the dev device.
   Never run both machines against the number at once.
5. Verify a reminder fires and a calendar write lands.

**Always-on operations on the Mac mini:**
- Disable system sleep (Energy Saver) — a sleeping mini means missed messages and dead reminders.
- Start Docker on login (or run as a launchd service); compose uses `restart: unless-stopped` so the
  stack recovers from crashes and reboots.

---

## 13. Build milestones

1. **Plumbing** — WAHA up, two-way echo with the dedicated number, sender allow-list enforced.
   (Test against your own number first, not the family thread.)
2. **Brain** — Claude tool-use loop turning messages into structured intents; basic Q&A and items.
3. **Calendar + reminders** — create/read on the shared calendar; reminders that fire and survive a restart.
4. **Email drafts** — per-user Gmail OAuth (opt-in), draft-on-request.
5. **Automation framework** — allow-list + confirm-first + consent recording.
6. **Cutover** — move to the Mac mini per §12 and run it as the live host.

---

## 14. Security & privacy

- Dedicated WhatsApp number on a separate SIM; never a personal number.
- Sender allow-list: ignore everyone not explicitly added.
- `.env` gitignored; Anthropic key, Google service-account JSON path, Gmail tokens, WAHA API key all live there.
- Service-account JSON and OAuth tokens stored outside the repo, mounted read-only.
- The Mac mini holds real family calendar/email access — treat its physical and network access seriously
  even though it's "just family."

---

## 15. Open decisions

- **WhatsApp layer:** WAHA + Python (recommended) vs. Baileys-direct (single-language Node). Pick before milestone 1.
- **Datastore:** SQLite (simplest, file-copy migration) vs. Postgres (upgrade path). Default SQLite.
- **Which Google account** owns and shares the family calendar.
- **Timezone handling:** single family default vs. per-member overrides — confirm the default.
- **Confirmation expiry:** how long a `pending_confirmation` stays valid before it lapses.

---

## 16. Risks & mitigations (summary)

| Risk | Mitigation |
|---|---|
| WhatsApp ToS / ban | Dedicated number, low volume, allow-list, human-like cadence, no bulk |
| Session drops | WAHA auto-reconnect; documented re-pair procedure; alert on disconnect |
| Unofficial lib breaks on a WhatsApp change | Pin versions; watch upstream; accept occasional maintenance |
| Reminders lost after restart | Persistent jobstore + rehydrate on boot; mini never sleeps |
| Secret leakage | gitignore, service account, tokens outside repo, restricted host access |
| Accidental email send | Draft-only hard rule; no send capability exists in code |
