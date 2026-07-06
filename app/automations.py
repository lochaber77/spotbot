"""Automation framework (M5): an allow-list of pre-agreed automations.

Each automation is a declared `AutomationSpec` in the code registry below; a
row in the `automations` table carries its operational metadata (enabled,
requires_confirmation, consent_recorded_at). The brain exposes them via the
`propose_automation` tool and routes anything consequential through the same
confirm-first `PendingConfirmation` mechanism used by calendar/email (spec §10).

Semantics:
- An automation is *enabled by default*; it's off only if its DB row explicitly
  says so. So a freshly-registered automation works before `sync_db()` runs.
- `requires_confirmation=True` (the default for anything affecting others) routes
  through confirm-first; `False` executes directly (low-stakes, self-affecting).
- On execution we record consent (first-time timestamp) on the automation's row.

To add an automation: append an `AutomationSpec` to `AUTOMATIONS`. Give it a
clear `usage` string (the brain shows it to Claude) and an `execute(member, args)`
that returns a short human-readable result string.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

import db
import scheduler

log = logging.getLogger("app.automations")


class AutomationSpec:
    def __init__(self, name, description, usage, requires_confirmation, execute):
        self.name = name
        self.description = description
        self.usage = usage  # shown to Claude: how to call it / what args it takes
        self.requires_confirmation = requires_confirmation
        self.execute = execute  # execute(member, args: dict) -> str


# --- Worked example automation ---------------------------------------------

def _execute_broadcast_reminder(member, args: dict) -> str:
    """Set the same reminder for every active family member (affects others)."""
    text = args["text"]
    tz = ZoneInfo(member.timezone)
    dt = datetime.fromisoformat(args["fire_at_iso"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    fire_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)

    created = []
    with db.session_scope() as session:
        members = session.scalars(
            select(db.FamilyMember).where(db.FamilyMember.active.is_(True))
        ).all()
        for m in members:
            reminder = db.Reminder(
                member_id=m.id, text=text, fire_at_utc=fire_utc, status=db.STATUS_SCHEDULED
            )
            session.add(reminder)
            session.flush()
            created.append(reminder.id)

    for reminder_id in created:
        scheduler.schedule_reminder(reminder_id, fire_utc)
    return f"Set a reminder for {len(created)} family member(s): '{text}'."


AUTOMATIONS = {
    spec.name: spec
    for spec in [
        AutomationSpec(
            name="broadcast_reminder",
            description="Remind every active family member about something at a set time.",
            usage="broadcast_reminder(text, fire_at_iso) — fire_at_iso is an absolute "
            "local wall-clock time (ISO 8601, no offset).",
            requires_confirmation=True,  # affects others
            execute=_execute_broadcast_reminder,
        ),
    ]
}


# --- Registry / DB helpers --------------------------------------------------

def _row(session, name):
    return session.scalar(select(db.Automation).where(db.Automation.name == name))


def get_enabled(name: str):
    """Return the spec for an enabled automation, or None."""
    spec = AUTOMATIONS.get(name)
    if spec is None:
        return None
    with db.session_scope() as session:
        row = _row(session, name)
        if row is not None and not row.enabled:
            return None
    return spec


def enabled_specs() -> list:
    return [s for s in AUTOMATIONS.values() if get_enabled(s.name) is not None]


def describe_enabled() -> str:
    """A short listing for the system prompt, or '' if none are enabled."""
    specs = enabled_specs()
    if not specs:
        return ""
    lines = [f"- {s.usage} {s.description}" for s in specs]
    return "Available automations (call via propose_automation):\n" + "\n".join(lines)


def record_consent(name: str) -> None:
    """Stamp first-time consent for an automation (upserting its row)."""
    spec = AUTOMATIONS.get(name)
    with db.session_scope() as session:
        row = _row(session, name)
        if row is None:
            row = db.Automation(
                name=name,
                description=spec.description if spec else name,
                requires_confirmation=spec.requires_confirmation if spec else True,
                enabled=True,
            )
            session.add(row)
        if row.consent_recorded_at is None:
            row.consent_recorded_at = db.utcnow()


def sync_db() -> None:
    """Upsert a row for each registered automation (metadata only; keeps flags)."""
    with db.session_scope() as session:
        for spec in AUTOMATIONS.values():
            row = _row(session, spec.name)
            if row is None:
                session.add(
                    db.Automation(
                        name=spec.name,
                        description=spec.description,
                        requires_confirmation=spec.requires_confirmation,
                        enabled=True,
                    )
                )
            else:
                row.description = spec.description
                row.requires_confirmation = spec.requires_confirmation
    log.info("Synced %d automation(s)", len(AUTOMATIONS))
