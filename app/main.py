"""FastAPI app: receives WAHA webhooks and replies.

Milestone 1 scope:
  - receive inbound messages from WAHA
  - enforce the sender allow-list (ignore everyone else, ignore groups)
  - generate a reply with Claude and send it back

NOTE: confirm the inbound payload shape against your WAHA version. For the
'message' event WAHA posts: {"event": "message", "payload": {"from": "...@c.us",
"body": "...", "fromMe": false}}. Adjust the parsing below if your version differs.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

import automations
import config
import db
import scheduler
import whatsapp
from brain import generate_reply

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init the datastore and start the scheduler. The persistent jobstore
    # rehydrates any pending reminder jobs, so reminders survive a restart.
    db.init_db()
    automations.sync_db()
    scheduler.start()
    log.info("Startup complete. Allow-listed numbers: %d", len(config.ALLOWED_NUMBERS))
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "jobs": len(scheduler.scheduler.get_jobs())}


@app.post("/webhook")
async def webhook(request: Request):
    event = await request.json()

    if event.get("event") != "message":
        return {"ignored": "not a message event"}

    payload = event.get("payload", {})

    # Skip our own outbound messages to avoid loops.
    if payload.get("fromMe"):
        return {"ignored": "fromMe"}

    raw_from = payload.get("from", "")  # e.g. '447700900000@c.us'

    # Ignore group chats for now (they end in '@g.us').
    if raw_from.endswith("@g.us"):
        return {"ignored": "group chat"}

    # The allow-list gate matches on the digits/id before the '@'. But WhatsApp
    # may address a sender by phone ('…@c.us') or by privacy id ('…@lid'), and
    # replies/reminders must go back to that *exact* chat — so we carry the full
    # chat id (raw_from) as the member's identity and send target.
    number = raw_from.split("@", 1)[0]
    body = (payload.get("body") or "").strip()

    # The allow-list gate: only known family numbers get a response.
    if number not in config.ALLOWED_NUMBERS:
        log.info("Ignoring message from non-allow-listed number: %s", number)
        return {"ignored": "not allow-listed"}

    if not body:
        return {"ignored": "empty body"}

    log.info("Message from %s: %s", number, body)

    try:
        reply = await generate_reply(body, raw_from)
        await whatsapp.send_text(raw_from, reply)
    except Exception:
        log.exception("Failed to handle message")
        await whatsapp.send_text(
            raw_from, "Sorry — something went wrong on my end. Try again in a moment."
        )

    return {"ok": True}
