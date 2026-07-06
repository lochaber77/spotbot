"""Tests for the M3 shared-calendar tools and the confirm-first flow.

Google Calendar is stubbed (no network, no credentials): we replace
gcal.create_event / gcal.list_events and flip config.CALENDAR_ENABLED on.
"""
import os
import tempfile
import types

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALLOWED_NUMBERS", "447700900000")
os.environ.setdefault("TZ", "Europe/London")
_TMP = tempfile.mkdtemp(prefix="spotbot-cal-test-")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "app.sqlite"))

from datetime import datetime, timezone  # noqa: E402

import brain  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import gcal  # noqa: E402

NUMBER = "447700900000"


def _member():
    db.init_db()
    with db.session_scope() as session:
        m = db.get_or_create_member(session, NUMBER)
        mid = m.id
    return types.SimpleNamespace(id=mid, timezone=config.TZ)


def _enable_calendar(monkeypatch, *, created=None, listed=None):
    monkeypatch.setattr(config, "CALENDAR_ENABLED", True)
    calls = {"create": [], "list": []}

    def fake_create(title, start_utc, end_utc, attendees=None, description=None):
        calls["create"].append((title, start_utc, end_utc, attendees))
        return created or {"id": "evt_123", "htmlLink": "https://cal/evt_123"}

    def fake_list(time_min_utc, time_max_utc, max_results=25):
        calls["list"].append((time_min_utc, time_max_utc))
        return listed or []

    monkeypatch.setattr(gcal, "create_event", fake_create)
    monkeypatch.setattr(gcal, "list_events", fake_list)
    return calls


def test_create_is_confirm_first_then_executes(monkeypatch):
    member = _member()
    calls = _enable_calendar(monkeypatch)

    # Propose — this must NOT create the event yet.
    msg = brain._tool_create_calendar_event(
        member, {"title": "Dentist", "start_iso": "2026-07-10T15:00:00", "end_iso": "2026-07-10T16:00:00"}
    )
    assert "confirmation_id=" in msg
    assert calls["create"] == [], "event must not be created before confirmation"

    pending = brain._pending_for(member.id)
    assert pending is not None and pending["kind"] == "calendar_event"
    cid = pending["id"]

    # Approve — now it creates the event and caches it.
    out = brain._tool_resolve_confirmation(member, {"confirmation_id": cid, "approve": True})
    assert "Added 'Dentist'" in out
    assert len(calls["create"]) == 1
    title, start_utc, end_utc, _ = calls["create"][0]
    # 15:00 BST (July, UTC+1) -> 14:00 UTC.
    assert title == "Dentist" and start_utc == datetime(2026, 7, 10, 14, 0)

    from sqlalchemy import select

    with db.session_scope() as session:
        ev = session.scalars(select(db.CalendarEvent)).first()
        pc = session.get(db.PendingConfirmation, cid)
        assert ev is not None and ev.google_event_id == "evt_123"
        assert pc.status == db.PC_EXECUTED

    # Re-resolving a settled confirmation is a no-op message.
    again = brain._tool_resolve_confirmation(member, {"confirmation_id": cid, "approve": True})
    assert "already" in again


def test_decline_creates_nothing(monkeypatch):
    member = _member()
    calls = _enable_calendar(monkeypatch)
    brain._tool_create_calendar_event(
        member, {"title": "Trip", "start_iso": "2026-07-11T09:00:00", "end_iso": "2026-07-11T10:00:00"}
    )
    cid = brain._pending_for(member.id)["id"]

    out = brain._tool_resolve_confirmation(member, {"confirmation_id": cid, "approve": False})
    assert "cancelled" in out.lower()
    assert calls["create"] == []
    with db.session_scope() as session:
        from sqlalchemy import select

        pc = session.get(db.PendingConfirmation, cid)
        assert pc.status == db.PC_DECLINED
        assert session.scalars(select(db.CalendarEvent).where(db.CalendarEvent.title == "Trip")).first() is None


def test_expired_confirmation_is_rejected(monkeypatch):
    member = _member()
    _enable_calendar(monkeypatch)
    brain._tool_create_calendar_event(
        member, {"title": "Old", "start_iso": "2026-07-12T09:00:00", "end_iso": "2026-07-12T10:00:00"}
    )
    cid = brain._pending_for(member.id)["id"]
    # Force it to have already expired.
    with db.session_scope() as session:
        session.get(db.PendingConfirmation, cid).expires_at = datetime(2000, 1, 1)

    out = brain._tool_resolve_confirmation(member, {"confirmation_id": cid, "approve": True})
    assert "expired" in out.lower()
    assert brain._pending_for(member.id) is None  # no longer offered


def test_list_schedule_formats_events(monkeypatch):
    member = _member()
    _enable_calendar(
        monkeypatch,
        listed=[
            {"id": "e1", "summary": "Swimming", "start": "2026-07-10T18:00:00+01:00", "htmlLink": ""},
            {"id": "e2", "summary": "Bank holiday", "start": "2026-08-31", "htmlLink": ""},
        ],
    )
    out = brain._tool_list_schedule(member, {})
    assert "Swimming" in out and "Bank holiday" in out
    assert "6:00 PM" in out  # 18:00 +01:00 shown in Europe/London


def test_calendar_disabled_message(monkeypatch):
    member = _member()
    monkeypatch.setattr(config, "CALENDAR_ENABLED", False)
    assert "isn't configured" in brain._tool_list_schedule(member, {})
    assert "isn't configured" in brain._tool_create_calendar_event(
        member, {"title": "X", "start_iso": "2026-07-10T15:00:00", "end_iso": "2026-07-10T16:00:00"}
    )
