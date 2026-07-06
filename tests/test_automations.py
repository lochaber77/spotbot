"""Tests for the M5 automation framework: registry, confirm-first, consent.

Uses the built-in `broadcast_reminder` example. Scheduler jobs are exercised
for real (in-memory), which also proves the automation reaches the scheduler.
"""
import asyncio
import os
import tempfile
import types

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALLOWED_NUMBERS", "447700900000")
os.environ.setdefault("TZ", "Europe/London")
_TMP = tempfile.mkdtemp(prefix="spotbot-auto-test-")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "app.sqlite"))

from sqlalchemy import select  # noqa: E402

import automations  # noqa: E402
import brain  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import scheduler  # noqa: E402

NUMBER = "447700900000"
NUMBER2 = "447700900001"


def _two_members():
    db.init_db()
    automations.sync_db()
    with db.session_scope() as session:
        a = db.get_or_create_member(session, NUMBER)
        b = db.get_or_create_member(session, NUMBER2)
        aid = a.id
    return types.SimpleNamespace(id=aid, timezone=config.TZ, whatsapp_number=NUMBER)


def test_registered_and_described():
    _two_members()
    assert "broadcast_reminder" in automations.AUTOMATIONS
    assert automations.get_enabled("broadcast_reminder") is not None
    assert "broadcast_reminder" in brain._system_prompt(
        types.SimpleNamespace(id=1, timezone=config.TZ, whatsapp_number=NUMBER)
    )


def test_unknown_automation_rejected():
    member = _two_members()
    out = brain._tool_propose_automation(member, {"name": "launch_missiles", "args": {}})
    assert "no enabled automation" in out


def test_confirm_first_runs_for_all_members_and_records_consent():
    asyncio.run(_flow())


async def _flow():
    member = _two_members()
    scheduler.start()

    # Consequential automation → proposal only, nothing scheduled yet.
    msg = brain._tool_propose_automation(
        member,
        {"name": "broadcast_reminder", "args": {"text": "bins out", "fire_at_iso": "2026-12-01T19:00:00"}},
    )
    assert "confirmation_id=" in msg
    with db.session_scope() as session:
        assert session.scalars(select(db.Reminder)).all() == []

    cid = brain._pending_for(member.id)["id"]
    out = brain._tool_resolve_confirmation(member, {"confirmation_id": cid, "approve": True})
    assert "2 family member" in out

    with db.session_scope() as session:
        reminders = session.scalars(select(db.Reminder)).all()
        assert len(reminders) == 2  # one per active member
        pc = session.get(db.PendingConfirmation, cid)
        assert pc.status == db.PC_EXECUTED
        auto = session.scalar(select(db.Automation).where(db.Automation.name == "broadcast_reminder"))
        assert auto.consent_recorded_at is not None  # consent recorded on execution

    # Both reminders got scheduled jobs.
    job_ids = {j.id for j in scheduler.scheduler.get_jobs()}
    assert {str(r.id) for r in reminders} <= job_ids

    # Leave the module scheduler stopped so the next test (its own event loop)
    # rebinds cleanly instead of touching this closed loop.
    scheduler.scheduler.shutdown(wait=False)


def test_disabled_automation_not_offered():
    member = _two_members()
    with db.session_scope() as session:
        session.scalar(select(db.Automation).where(db.Automation.name == "broadcast_reminder")).enabled = False
    assert automations.get_enabled("broadcast_reminder") is None
    out = brain._tool_propose_automation(
        member, {"name": "broadcast_reminder", "args": {"text": "x", "fire_at_iso": "2026-12-01T19:00:00"}}
    )
    assert "no enabled automation" in out
    # re-enable so other tests/ordering aren't affected
    with db.session_scope() as session:
        session.scalar(select(db.Automation).where(db.Automation.name == "broadcast_reminder")).enabled = True
