"""APScheduler wiring for proactive reminders.

Key design points (the critical requirement of this slice):

* Jobs are stored in a SQLAlchemy jobstore pointing at the *same* SQLite file as
  the app data, so they survive a restart and are rehydrated on boot.
* A reminder's scheduled job id IS the reminder id (as a string). That makes
  (re)scheduling idempotent and lets cancel remove the exact job.
* `coalesce=True` + a generous `misfire_grace_time` mean a reminder whose fire
  time passed while the app was down still fires exactly once on restart,
  instead of either vanishing or firing repeatedly.
* The scheduler is an AsyncIOScheduler so the fire callback can `await` the
  outbound WhatsApp call. The callback runs outside any request, so it opens its
  own DB session and makes its own WAHA call.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from . import db, whatsapp
from .config import settings

log = logging.getLogger(__name__)

# One hour: a reminder that should have fired during downtime still fires once.
MISFIRE_GRACE_SECONDS = 3600

jobstores = {"default": SQLAlchemyJobStore(url=settings.db_url, engine=db.engine)}

scheduler = AsyncIOScheduler(
    jobstores=jobstores,
    job_defaults={"coalesce": True, "misfire_grace_time": MISFIRE_GRACE_SECONDS},
    timezone="UTC",
)


def start() -> None:
    if not scheduler.running:
        scheduler.start()
        log.info("Scheduler started; %d job(s) rehydrated", len(scheduler.get_jobs()))


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _trigger_for(fire_at_utc: datetime, recurrence: Optional[str]):
    """Build an APScheduler trigger. One-off unless a recurrence is given."""
    if not recurrence:
        return DateTrigger(run_date=fire_at_utc, timezone=timezone.utc)
    rec = recurrence.strip().lower()
    if rec == "daily":
        return CronTrigger(hour=fire_at_utc.hour, minute=fire_at_utc.minute, timezone=timezone.utc)
    if rec == "weekly":
        return CronTrigger(
            day_of_week=fire_at_utc.weekday(),
            hour=fire_at_utc.hour,
            minute=fire_at_utc.minute,
            timezone=timezone.utc,
        )
    # Unknown recurrence -> treat as one-off rather than guessing.
    log.warning("Unknown recurrence %r; scheduling one-off", recurrence)
    return DateTrigger(run_date=fire_at_utc, timezone=timezone.utc)


def schedule_reminder(reminder_id: int, fire_at_utc: datetime, recurrence: Optional[str] = None) -> None:
    """Register (or replace) the job for a reminder.

    `fire_at_utc` may be naive (assumed UTC) or aware; we normalise to aware UTC.
    """
    if fire_at_utc.tzinfo is None:
        fire_at_utc = fire_at_utc.replace(tzinfo=timezone.utc)
    scheduler.add_job(
        fire_reminder,
        trigger=_trigger_for(fire_at_utc, recurrence),
        args=[reminder_id],
        id=str(reminder_id),
        replace_existing=True,
    )
    log.info("Scheduled reminder %s for %s (recurrence=%s)", reminder_id, fire_at_utc.isoformat(), recurrence)


def cancel_reminder_job(reminder_id: int) -> None:
    try:
        scheduler.remove_job(str(reminder_id))
        log.info("Removed job for reminder %s", reminder_id)
    except JobLookupError:
        log.info("No live job for reminder %s (already fired/removed)", reminder_id)


async def fire_reminder(reminder_id: int) -> None:
    """Job callback: send the reminder and record it.

    Runs outside the request lifecycle, so it owns its DB session and WAHA call.
    """
    # 1. Read what we need, then close the session before any network call.
    with db.session_scope() as session:
        reminder = session.get(db.Reminder, reminder_id)
        if reminder is None:
            log.warning("fire_reminder: reminder %s no longer exists", reminder_id)
            return
        if reminder.status == db.STATUS_CANCELLED:
            log.info("fire_reminder: reminder %s was cancelled; skipping", reminder_id)
            return
        member = session.get(db.FamilyMember, reminder.member_id)
        if member is None or not member.active:
            log.warning("fire_reminder: member for reminder %s missing/inactive", reminder_id)
            return
        chat_id = whatsapp.to_chat_id(member.whatsapp_number)
        text = f"⏰ Reminder: {reminder.text}"
        recurring = bool(reminder.recurrence)

    # 2. Send. A one-off DateTrigger job is removed by APScheduler after it runs,
    #    so a failure here won't re-fire; recording after the send keeps the row
    #    honest about what actually went out.
    await whatsapp.send_text(chat_id, text)

    # 3. Record the send.
    with db.session_scope() as session:
        reminder = session.get(db.Reminder, reminder_id)
        if reminder is not None:
            reminder.sent_at = db.utcnow()
            if not recurring:
                reminder.status = db.STATUS_SENT
    log.info("Fired reminder %s to %s", reminder_id, chat_id)
