"""Google Calendar client (M3): one shared calendar via a service account.

The family shares a single calendar; the bot authenticates as a Google service
account that's been granted "Make changes to events" on it — no per-person OAuth,
no token-refresh to babysit (see spec §8).

The Google libraries are imported lazily inside `_service()` so the rest of the
app (and the test suite, which stubs these functions) doesn't require them to be
installed or the credentials to be present. Times cross this boundary as naive
UTC datetimes (the app-wide convention) and are formatted to RFC 3339 here.
"""
import logging
from datetime import datetime, timezone

import config

log = logging.getLogger("app.gcal")

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_service_singleton = None


def _service():
    global _service_singleton
    if _service_singleton is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=_SCOPES
        )
        _service_singleton = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service_singleton


def _rfc3339(dt_utc: datetime) -> str:
    """Naive-UTC datetime -> RFC 3339 with a 'Z' suffix."""
    return dt_utc.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def create_event(title, start_utc, end_utc, attendees=None, description=None) -> dict:
    """Create an event on the shared calendar. Returns {id, htmlLink}."""
    body = {
        "summary": title,
        "start": {"dateTime": _rfc3339(start_utc), "timeZone": "UTC"},
        "end": {"dateTime": _rfc3339(end_utc), "timeZone": "UTC"},
    }
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    event = (
        _service()
        .events()
        .insert(calendarId=config.GOOGLE_CALENDAR_ID, body=body)
        .execute()
    )
    return {"id": event["id"], "htmlLink": event.get("htmlLink")}


def list_events(time_min_utc, time_max_utc, max_results=25) -> list[dict]:
    """List events in [time_min_utc, time_max_utc). Returns simplified dicts.

    Each item: {id, summary, start, htmlLink}. `start` is the raw Google value —
    an RFC 3339 dateTime (timed events) or a YYYY-MM-DD date (all-day) — which the
    caller localises for display.
    """
    result = (
        _service()
        .events()
        .list(
            calendarId=config.GOOGLE_CALENDAR_ID,
            timeMin=_rfc3339(time_min_utc),
            timeMax=_rfc3339(time_max_utc),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        )
        .execute()
    )
    out = []
    for event in result.get("items", []):
        start = event.get("start", {})
        out.append(
            {
                "id": event["id"],
                "summary": event.get("summary", "(no title)"),
                "start": start.get("dateTime") or start.get("date"),
                "htmlLink": event.get("htmlLink"),
            }
        )
    return out
