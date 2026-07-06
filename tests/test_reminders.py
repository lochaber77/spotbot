"""Regression tests for the reminders vertical slice.

These exercise the slice end to end without hitting Claude or WAHA:
timezone conversion, the set/list/cancel tools, the persistent-jobstore
restart-survival guarantee, and the fire callback (with WAHA stubbed).

Run: PYTHONPATH=app pytest -q
"""
import asyncio
import os
import tempfile
import types

# Configure the environment BEFORE importing the app modules: config reads these
# at import time and builds DB_URL from DB_PATH. Point the DB at an isolated temp
# file so the suite never touches real data.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALLOWED_NUMBERS", "447700900000")
os.environ.setdefault("TZ", "Europe/London")
_TMP = tempfile.mkdtemp(prefix="spotbot-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_PATH"] = os.path.join(_TMP, "app.sqlite")

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # noqa: E402
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from sqlalchemy import select  # noqa: E402

import brain  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import scheduler  # noqa: E402

NUMBER = "447700900000"


def _job_ids():
    return [j.id for j in scheduler.scheduler.get_jobs()]


def test_tz_conversion_dst_and_standard():
    # June: Europe/London is BST (UTC+1), so 07:00 local -> 06:00 UTC.
    summer = brain._local_iso_to_utc("2026-06-20T07:00:00", "Europe/London")
    assert summer.hour == 6 and summer.tzinfo is None
    # January: GMT (UTC+0), so 07:00 local -> 07:00 UTC.
    winter = brain._local_iso_to_utc("2026-01-20T07:00:00", "Europe/London")
    assert winter.hour == 7
    # Round-trips back to local wall-clock for display.
    assert "7:00 AM" in brain._utc_to_local_str(summer, "Europe/London")


def test_reminder_lifecycle():
    asyncio.run(_lifecycle())


async def _lifecycle():
    db.init_db()
    scheduler.start()

    with db.session_scope() as session:
        member_row = db.get_or_create_member(session, NUMBER)
        member_id = member_row.id
    # The brain hands tools a detached snapshot; mimic that.
    member = types.SimpleNamespace(id=member_id, timezone=config.TZ)

    # --- set ---
    msg = brain._tool_set_reminder(
        member, {"text": "take the bins out", "fire_at_iso": "2026-12-01T07:00:00"}
    )
    assert "Created reminder" in msg

    with db.session_scope() as session:
        reminder = session.scalars(select(db.Reminder)).first()
        rid = reminder.id
        assert reminder.status == db.STATUS_SCHEDULED
    assert str(rid) in _job_ids(), "scheduling must register a job whose id == reminder id"

    # --- list ---
    listed = brain._tool_list_reminders(member, {})
    assert f"#{rid}" in listed and "take the bins out" in listed

    # --- restart survival: rebuild the scheduler from the same jobstore ---
    scheduler.scheduler.shutdown(wait=False)
    fresh = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=config.DB_URL, engine=db.engine)},
        job_defaults={"coalesce": True, "misfire_grace_time": scheduler.MISFIRE_GRACE_SECONDS},
        timezone="UTC",
    )
    scheduler.scheduler = fresh  # later tool calls use the module global
    fresh.start()
    assert str(rid) in _job_ids(), "pending reminder job must survive a restart"

    # --- fire (WAHA stubbed) ---
    sent = []

    async def fake_send(number, text):
        sent.append((number, text))

    scheduler.whatsapp.send_text = fake_send
    await scheduler.fire_reminder(rid)
    assert len(sent) == 1 and sent[0][0] == NUMBER
    assert "take the bins out" in sent[0][1]
    with db.session_scope() as session:
        reminder = session.get(db.Reminder, rid)
        assert reminder.status == db.STATUS_SENT and reminder.sent_at is not None

    # --- cancel a second reminder ---
    brain._tool_set_reminder(member, {"text": "dentist", "fire_at_iso": "2026-12-02T09:00:00"})
    with db.session_scope() as session:
        second = session.scalars(
            select(db.Reminder).where(db.Reminder.text == "dentist")
        ).first()
        rid2 = second.id
    assert str(rid2) in _job_ids()

    out = brain._tool_cancel_reminder(member, {"reminder_id": rid2})
    assert "Cancelled" in out
    assert str(rid2) not in _job_ids(), "cancel must remove the job"
    with db.session_scope() as session:
        assert session.get(db.Reminder, rid2).status == db.STATUS_CANCELLED

    fresh.shutdown(wait=False)


def test_cancel_unknown_reminder_is_safe():
    db.init_db()
    member = types.SimpleNamespace(id=999999, timezone=config.TZ)
    out = brain._tool_cancel_reminder(member, {"reminder_id": 123456789})
    assert "no reminder" in out.lower()
