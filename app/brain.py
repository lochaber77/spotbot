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
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from sqlalchemy import select

import config
import db
import gcal
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
    "absolute time you scheduled so they can sanity-check it."
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


# Calendar tools are only offered when the shared calendar is configured.
CALENDAR_TOOLS = [
    {
        "name": "create_calendar_event",
        "description": (
            "Propose a new event on the family's shared calendar. This affects the "
            "whole family, so it is CONFIRM-FIRST: this records a proposal and does "
            "NOT create the event. Tell the user what you're about to add and ask "
            "them to confirm; the event is only created once they say yes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title."},
                "start_iso": {
                    "type": "string",
                    "description": (
                        "Start as absolute local wall-clock time, ISO 8601 with no "
                        "offset, e.g. '2026-07-10T15:00:00'. Member's timezone."
                    ),
                },
                "end_iso": {
                    "type": "string",
                    "description": "End time, same format as start_iso.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional attendee email addresses.",
                },
            },
            "required": ["title", "start_iso", "end_iso"],
        },
    },
    {
        "name": "list_schedule",
        "description": "Read upcoming events from the family's shared calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {
                    "type": "string",
                    "description": "Optional window start (local ISO 8601). Defaults to now.",
                },
                "end_iso": {
                    "type": "string",
                    "description": "Optional window end (local ISO 8601). Defaults to 7 days out.",
                },
            },
        },
    },
    {
        "name": "resolve_confirmation",
        "description": (
            "Approve or decline a pending confirmation (its id is given in the "
            "system prompt when one is awaiting the user's yes/no)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirmation_id": {"type": "integer", "description": "The pending confirmation id."},
                "approve": {"type": "boolean", "description": "true to confirm, false to decline."},
            },
            "required": ["confirmation_id", "approve"],
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


# --- Calendar tools (confirm-first for writes) ------------------------------

def _tool_create_calendar_event(member, args: dict) -> str:
    if not config.CALENDAR_ENABLED:
        return "ERROR: the shared calendar isn't configured yet."
    title = args["title"]
    try:
        start_utc = _local_iso_to_utc(args["start_iso"], member.timezone)
        end_utc = _local_iso_to_utc(args["end_iso"], member.timezone)
    except ValueError as exc:
        return f"ERROR: could not parse start/end ({exc})."

    payload = {
        "title": title,
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "attendees": args.get("attendees") or [],
    }
    with db.session_scope() as session:
        pending = db.PendingConfirmation(
            member_id=member.id,
            kind="calendar_event",
            payload=json.dumps(payload),
            status=db.PC_PENDING,
            expires_at=db.utcnow() + timedelta(minutes=config.CONFIRMATION_TTL_MINUTES),
        )
        session.add(pending)
        session.flush()
        cid = pending.id

    when = _utc_to_local_str(start_utc, member.timezone)
    return (
        f"PROPOSED (confirmation_id={cid}): shared calendar event '{title}' on {when}. "
        "This affects the whole family — ask the user to confirm before it is created; "
        "do not create it yet."
    )


def _tool_list_schedule(member, args: dict) -> str:
    if not config.CALENDAR_ENABLED:
        return "ERROR: the shared calendar isn't configured yet."
    try:
        start_utc = (
            _local_iso_to_utc(args["start_iso"], member.timezone)
            if args.get("start_iso")
            else db.utcnow()
        )
        end_utc = (
            _local_iso_to_utc(args["end_iso"], member.timezone)
            if args.get("end_iso")
            else start_utc + timedelta(days=7)
        )
    except ValueError as exc:
        return f"ERROR: could not parse date range ({exc})."

    try:
        events = gcal.list_events(start_utc, end_utc)
    except Exception:  # network / API error — tell Claude, don't crash the loop
        log.exception("calendar list failed")
        return "ERROR: couldn't read the calendar (API error)."

    if not events:
        return "Nothing on the shared calendar in that window."
    lines = []
    for event in events:
        raw = event["start"]
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(member.timezone))
            when = dt.astimezone(ZoneInfo(member.timezone)).strftime("%a %d %b, %-I:%M %p")
        except ValueError:
            when = raw  # all-day date (YYYY-MM-DD)
        lines.append(f"• {event['summary']} — {when}")
    return "\n".join(lines)


def _tool_resolve_confirmation(member, args: dict) -> str:
    cid = args["confirmation_id"]
    approve = bool(args.get("approve", False))

    # Load + validate + record the decision; keep the DB session closed during
    # the outbound calendar call.
    with db.session_scope() as session:
        pending = session.get(db.PendingConfirmation, cid)
        if pending is None or pending.member_id != member.id:
            return f"ERROR: no pending confirmation #{cid} for you."
        if pending.status != db.PC_PENDING:
            return f"Confirmation #{cid} was already {pending.status}."
        expired = pending.expires_at <= db.utcnow()
        kind = pending.kind
        payload = json.loads(pending.payload)
        if expired or not approve:
            pending.status = db.PC_DECLINED

    if expired:
        return f"That request (#{cid}) expired — please ask again."
    if not approve:
        return "Okay, cancelled — nothing was added."

    if kind != "calendar_event":
        return f"ERROR: unknown confirmation kind {kind!r}."

    start_utc = datetime.fromisoformat(payload["start_utc"])
    end_utc = datetime.fromisoformat(payload["end_utc"])
    try:
        event = gcal.create_event(
            payload["title"], start_utc, end_utc, attendees=payload.get("attendees") or None
        )
    except Exception:
        log.exception("calendar create failed")
        return "ERROR: couldn't create the event (calendar API error)."

    with db.session_scope() as session:
        pending = session.get(db.PendingConfirmation, cid)
        if pending is not None:
            pending.status = db.PC_EXECUTED
        session.add(
            db.CalendarEvent(
                google_event_id=event["id"],
                title=payload["title"],
                start_utc=start_utc,
                end_utc=end_utc,
                attendees=",".join(payload.get("attendees") or []) or None,
                created_by=member.id,
                html_link=event.get("htmlLink"),
            )
        )

    when = _utc_to_local_str(start_utc, member.timezone)
    return f"Added '{payload['title']}' to the family calendar for {when}."


_DISPATCH = {
    "set_reminder": _tool_set_reminder,
    "list_reminders": _tool_list_reminders,
    "cancel_reminder": _tool_cancel_reminder,
    "create_calendar_event": _tool_create_calendar_event,
    "list_schedule": _tool_list_schedule,
    "resolve_confirmation": _tool_resolve_confirmation,
}


def _pending_for(member_id: int):
    """The member's latest un-expired pending confirmation, or None."""
    with db.session_scope() as session:
        pending = session.scalars(
            select(db.PendingConfirmation)
            .where(
                db.PendingConfirmation.member_id == member_id,
                db.PendingConfirmation.status == db.PC_PENDING,
                db.PendingConfirmation.expires_at > db.utcnow(),
            )
            .order_by(db.PendingConfirmation.created_at.desc())
        ).first()
        if pending is None:
            return None
        return {"id": pending.id, "kind": pending.kind, "payload": json.loads(pending.payload)}


def _execute_tool(member, name: str, args: dict) -> str:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"ERROR: unknown tool {name!r}."
    try:
        return handler(member, args)
    except Exception:  # surface failure to Claude, don't crash the loop
        log.exception("Tool %s failed", name)
        return f"ERROR: tool {name} failed unexpectedly."


def _system_prompt(member, pending=None) -> str:
    now_local = datetime.now(ZoneInfo(member.timezone))
    parts = [SYSTEM_PROMPT]

    if config.CALENDAR_ENABLED:
        parts.append(
            "You can also read the family's shared calendar (list_schedule) and "
            "propose new events (create_calendar_event). Calendar events affect "
            "everyone, so they are CONFIRM-FIRST: after create_calendar_event, tell "
            "the user exactly what you'll add and ask them to confirm. Only once "
            "they say yes do you call resolve_confirmation to create it."
        )
    else:
        parts.append("You cannot read or create calendar events yet — that isn't configured.")
    parts.append("You cannot draft emails yet — that capability is coming.")

    if pending:
        p = pending["payload"]
        parts.append(
            f"There is a PENDING confirmation (confirmation_id={pending['id']}) "
            f"awaiting the user's yes/no — {pending['kind']}: '{p.get('title', '')}' "
            f"at {p.get('start_utc', '')} UTC. If the user approves in this message, "
            f"call resolve_confirmation(confirmation_id={pending['id']}, approve=true); "
            "if they decline, approve=false."
        )

    parts.append(
        f"Member timezone: {member.timezone}."
        f"\nCurrent local time: {now_local.strftime('%A %Y-%m-%d %H:%M %Z')}."
    )
    return "\n\n".join(parts)


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

    pending = _pending_for(member.id)
    system = _system_prompt(member, pending)
    tools = TOOLS + (CALENDAR_TOOLS if config.CALENDAR_ENABLED else [])
    messages = [{"role": "user", "content": message_text}]

    for _ in range(8):  # safety bound on tool round-trips
        resp = await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system,
            tools=tools,
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
