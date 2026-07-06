"""The Claude 'brain'.

Milestone 1 was a single-turn reply. Milestone 3 plugs in a tool-use loop for
reminders: Claude can set, list, and cancel reminders, which write to the
datastore and (de)register scheduler jobs. Calendar and email drafts are still
later milestones.

Time handling: we tell Claude the member's timezone and current local time, and
ask it to return an absolute local wall-clock time as ISO 8601 (no offset). We
localise that to the member's timezone and convert to UTC before storing. Store
UTC, display local — both conversions live here.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from sqlalchemy import select

import config
import db
import scheduler

log = logging.getLogger("app.brain")

client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = (
    "You are a helpful WhatsApp assistant for a family. You help organize and "
    "plan, answer questions, and keep things light and friendly. Replies are "
    "concise and suited to a chat app.\n\n"
    "You can set, list, and cancel personal reminders using the provided tools. "
    "Reminders are low-stakes and personal, so just do what's asked — no need to "
    "confirm first. When setting a reminder, resolve relative times like "
    "'tomorrow at 7am' or 'in 30 minutes' into an absolute local wall-clock time "
    "using the member's timezone and current time given below, and pass it as "
    "fire_at_iso. After using a tool, reply briefly and naturally, echoing the "
    "absolute time you scheduled so they can sanity-check it.\n\n"
    "You cannot yet create calendar events or draft emails — those capabilities "
    "are coming. If asked, say they're not wired up yet rather than pretending."
)

TOOLS = [
    {
        "name": "set_reminder",
        "description": (
            "Create a reminder for the member. Resolve any relative time into an "
            "absolute local wall-clock time using the member's timezone and "
            "current time from the system prompt."
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
                        "timezone offset, e.g. '2026-06-25T07:00:00'. Interpreted "
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
    dt = datetime.fromisoformat(fire_at_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))  # trust the member's timezone
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_to_local_str(dt_utc: datetime, tz_name: str) -> str:
    """Format a naive-UTC datetime in the member's local timezone for display."""
    aware = dt_utc.replace(tzinfo=timezone.utc)
    local = aware.astimezone(ZoneInfo(tz_name))
    return local.strftime("%a %d %b %Y, %-I:%M %p %Z")


# --- Tool implementations (member is a detached FamilyMember snapshot) -------

def _tool_set_reminder(member, args: dict) -> str:
    text = args["text"]
    recurrence = args.get("recurrence")
    try:
        fire_at_utc = _local_iso_to_utc(args["fire_at_iso"], member.timezone)
    except ValueError as exc:
        return f"ERROR: could not parse fire_at_iso ({exc})."

    with db.session_scope() as session:
        reminder = db.Reminder(
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


def _tool_list_reminders(member, args: dict) -> str:
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.Reminder)
            .where(db.Reminder.member_id == member.id, db.Reminder.status == db.STATUS_SCHEDULED)
            .order_by(db.Reminder.fire_at_utc)
        ).all()
        if not rows:
            return "No upcoming reminders."
        lines = []
        for r in rows:
            when = _utc_to_local_str(r.fire_at_utc, member.timezone)
            rec = f" (repeats {r.recurrence})" if r.recurrence else ""
            lines.append(f"#{r.id}: {r.text} — {when}{rec}")
        return "\n".join(lines)


def _tool_cancel_reminder(member, args: dict) -> str:
    reminder_id = args["reminder_id"]
    with db.session_scope() as session:
        reminder = session.get(db.Reminder, reminder_id)
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


def _execute_tool(member, name: str, args: dict) -> str:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"ERROR: unknown tool {name!r}."
    try:
        return handler(member, args)
    except Exception:  # surface failure to Claude, don't crash the loop
        log.exception("Tool %s failed", name)
        return f"ERROR: tool {name} failed unexpectedly."


def _system_prompt(member) -> str:
    now_local = datetime.now(ZoneInfo(member.timezone))
    return (
        SYSTEM_PROMPT
        + f"\n\nMember timezone: {member.timezone}."
        + f"\nCurrent local time: {now_local.strftime('%A %Y-%m-%d %H:%M %Z')}."
    )


async def generate_reply(message_text: str, sender_number: str) -> str:
    """Run the tool-use loop for one inbound message; return the text reply.

    Resolves (or creates) the family member for `sender_number`. The caller has
    already enforced the allow-list.
    """
    # Resolve the member and take a detached snapshot the tools can use without
    # holding a session open across Claude calls.
    with db.session_scope() as session:
        m = db.get_or_create_member(session, sender_number)
        session.add(db.Message(member_id=m.id, direction="in", text=message_text))
        member = db.FamilyMember(
            id=m.id,
            name=m.name,
            whatsapp_number=m.whatsapp_number,
            timezone=m.timezone,
            active=m.active,
        )

    system = _system_prompt(member)
    messages = [{"role": "user", "content": message_text}]

    for _ in range(8):  # safety bound on tool round-trips
        resp = await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            return "\n".join(b.text for b in resp.content if b.type == "text").strip() or "..."

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
