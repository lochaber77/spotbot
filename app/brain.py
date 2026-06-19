"""The 'brain': a Claude tool-use loop that drives the reminder tools.

Flow: call Claude with the tool definitions; while it returns tool_use blocks,
execute each (writing to the DB and scheduling jobs), feed back tool_result
blocks, and call again; finally return Claude's text reply.

Time handling: we tell Claude the member's IANA timezone and the current local
time, and ask it to return an absolute local wall-clock time as ISO 8601
(no offset). We then localise that to the member's timezone and convert to UTC
before storing. Storing UTC, displaying local — done in one place.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from sqlalchemy import select

from . import db, scheduler
from .config import settings
from .db import FamilyMember, Reminder

log = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=settings.anthropic_api_key)

TOOLS = [
    {
        "name": "set_reminder",
        "description": (
            "Create a reminder for the family member. Resolve any relative time "
            "(e.g. 'tomorrow at 7am', 'in 2 hours') into an absolute local "
            "wall-clock time using the member's current time and timezone given "
            "in the system prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "What to remind them about, phrased as the reminder body.",
                },
                "fire_at_iso": {
                    "type": "string",
                    "description": (
                        "Absolute local wall-clock time in ISO 8601 with no "
                        "timezone offset, e.g. '2026-06-20T07:00:00'. Interpreted "
                        "in the member's timezone."
                    ),
                },
                "recurrence": {
                    "type": "string",
                    "enum": ["daily", "weekly"],
                    "description": "Optional. Omit for a one-off reminder.",
                },
            },
            "required": ["text", "fire_at_iso"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List the member's upcoming (scheduled) reminders.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a reminder by its id (as shown by list_reminders).",
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "integer", "description": "The reminder's id."},
            },
            "required": ["reminder_id"],
        },
    },
]


# --- Time helpers -----------------------------------------------------------

def _local_iso_to_utc(fire_at_iso: str, tz_name: str) -> datetime:
    """Convert a local wall-clock ISO string to a naive UTC datetime."""
    tz = ZoneInfo(tz_name)
    dt = datetime.fromisoformat(fire_at_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)  # trust the member's timezone
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_to_local_str(dt_utc: datetime, tz_name: str) -> str:
    """Format a naive-UTC datetime in the member's local timezone for display."""
    aware = dt_utc.replace(tzinfo=timezone.utc)
    local = aware.astimezone(ZoneInfo(tz_name))
    return local.strftime("%a %d %b %Y, %-I:%M %p %Z")


# --- Tool implementations ---------------------------------------------------

def _tool_set_reminder(member: FamilyMember, args: dict) -> str:
    text = args["text"]
    recurrence = args.get("recurrence")
    try:
        fire_at_utc = _local_iso_to_utc(args["fire_at_iso"], member.timezone)
    except ValueError as exc:
        return f"ERROR: could not parse fire_at_iso ({exc})."

    with db.session_scope() as session:
        reminder = Reminder(
            member_id=member.id,
            text=text,
            fire_at_utc=fire_at_utc,
            recurrence=recurrence,
            status=db.STATUS_SCHEDULED,
        )
        session.add(reminder)
        session.flush()
        reminder_id = reminder.id

    # Schedule after the row exists so job id == reminder id.
    scheduler.schedule_reminder(reminder_id, fire_at_utc, recurrence)
    local = _utc_to_local_str(fire_at_utc, member.timezone)
    return (
        f"Created reminder #{reminder_id}: '{text}' at {local}"
        + (f" (repeats {recurrence})" if recurrence else "")
    )


def _tool_list_reminders(member: FamilyMember, args: dict) -> str:
    with db.session_scope() as session:
        rows = session.scalars(
            select(Reminder)
            .where(Reminder.member_id == member.id, Reminder.status == db.STATUS_SCHEDULED)
            .order_by(Reminder.fire_at_utc)
        ).all()
        if not rows:
            return "No upcoming reminders."
        lines = []
        for r in rows:
            when = _utc_to_local_str(r.fire_at_utc, member.timezone)
            rec = f" (repeats {r.recurrence})" if r.recurrence else ""
            lines.append(f"#{r.id}: {r.text} — {when}{rec}")
        return "\n".join(lines)


def _tool_cancel_reminder(member: FamilyMember, args: dict) -> str:
    reminder_id = args["reminder_id"]
    with db.session_scope() as session:
        reminder = session.get(Reminder, reminder_id)
        if reminder is None or reminder.member_id != member.id:
            return f"ERROR: no reminder #{reminder_id} found for you."
        if reminder.status == db.STATUS_CANCELLED:
            return f"Reminder #{reminder_id} was already cancelled."
        reminder.status = db.STATUS_CANCELLED
        text = reminder.text

    scheduler.cancel_reminder_job(reminder_id)
    return f"Cancelled reminder #{reminder_id}: '{text}'."


_DISPATCH = {
    "set_reminder": _tool_set_reminder,
    "list_reminders": _tool_list_reminders,
    "cancel_reminder": _tool_cancel_reminder,
}


def _execute_tool(member: FamilyMember, name: str, args: dict) -> str:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"ERROR: unknown tool {name!r}."
    try:
        return handler(member, args)
    except Exception:  # noqa: BLE001 - surface failure to Claude, don't crash the loop
        log.exception("Tool %s failed", name)
        return f"ERROR: tool {name} failed unexpectedly."


def _system_prompt(member: FamilyMember) -> str:
    tz = ZoneInfo(member.timezone)
    now_local = datetime.now(tz)
    return (
        "You are a helpful family WhatsApp assistant. You help family members "
        "manage personal reminders via the provided tools.\n\n"
        f"You are talking to: {member.name}.\n"
        f"Their timezone: {member.timezone}.\n"
        f"Current local time: {now_local.strftime('%A %Y-%m-%d %H:%M %Z')}.\n\n"
        "When the member asks to set a reminder, resolve relative phrases like "
        "'tomorrow at 7am' or 'in 30 minutes' into an absolute local wall-clock "
        "time and pass it as fire_at_iso (ISO 8601, no offset). Reminders are "
        "low-stakes and personal — just do what's asked, no need to confirm "
        "before acting. After using a tool, reply briefly and naturally, "
        "echoing the absolute time you scheduled so they can double-check it. "
        "Keep replies concise and friendly for a chat app."
    )


async def handle_message(member: FamilyMember, text: str) -> str:
    """Run the tool-use loop for one inbound message; return the text reply."""
    system = _system_prompt(member)
    messages: list[dict] = [{"role": "user", "content": text}]

    for _ in range(8):  # safety bound on tool round-trips
        resp = await client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = _execute_tool(member, block.name, block.input)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result}
                )
        messages.append({"role": "user", "content": tool_results})

    return "Sorry, I got stuck working on that — please try again."
