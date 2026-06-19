"""FastAPI entrypoint: WhatsApp webhook -> allow-list -> brain -> reply.

On startup we initialise the DB and start the scheduler; the persistent
jobstore rehydrates any pending reminder jobs so they still fire after a
restart. The allow-list gate from Milestone 1 is preserved unchanged.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from . import brain, db, scheduler, whatsapp
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start()  # rehydrates persisted jobs from the SQLAlchemy jobstore
    log.info("Startup complete. Allow-list size: %d", len(settings.allowed_numbers))
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="Family WhatsApp Assistant", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "jobs": len(scheduler.scheduler.get_jobs())}


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    body = await request.json()

    parsed = whatsapp.parse_inbound(body)
    if parsed is None:
        return {"status": "ignored"}
    chat_id, text = parsed

    # --- Allow-list gate (unchanged from Milestone 1) ---
    if not settings.is_allowed(chat_id):
        log.warning("Rejected message from non-allow-listed sender %s", chat_id)
        return {"status": "forbidden"}

    # Resolve the sender to a family member (create a minimal row if new).
    with db.session_scope() as session:
        member = db.get_or_create_member(session, chat_id, default_tz=settings.default_timezone)
        session.add(db.MessageLog(member_id=member.id, direction="in", text=text))
        # Detach a lightweight copy of what the brain needs.
        member_ctx = db.FamilyMember(
            id=member.id,
            name=member.name,
            whatsapp_number=member.whatsapp_number,
            timezone=member.timezone,
            active=member.active,
        )

    try:
        reply = await brain.handle_message(member_ctx, text)
    except Exception:  # noqa: BLE001
        log.exception("Brain failed handling message from %s", chat_id)
        reply = "Sorry, something went wrong on my end. Please try again."

    await whatsapp.send_text(chat_id, reply)
    with db.session_scope() as session:
        session.add(db.MessageLog(member_id=member_ctx.id, direction="out", text=reply))

    return {"status": "ok"}
