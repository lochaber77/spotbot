"""Tests for the M4 Gmail draft tool and the draft-only guardrail.

Gmail is stubbed (no network, no tokens): we replace gmail.has_credentials and
gmail.create_draft. One test also asserts the client has NO send capability.
"""
import os
import tempfile
import types

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALLOWED_NUMBERS", "447700900000")
os.environ.setdefault("TZ", "Europe/London")
_TMP = tempfile.mkdtemp(prefix="spotbot-email-test-")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "app.sqlite"))

import brain  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import gmail  # noqa: E402

NUMBER = "447700900000"


def _member():
    db.init_db()
    with db.session_scope() as session:
        m = db.get_or_create_member(session, NUMBER)
        mid = m.id
    return types.SimpleNamespace(id=mid, timezone=config.TZ, whatsapp_number=NUMBER)


def _enable_email(monkeypatch, created=None):
    monkeypatch.setattr(gmail, "has_credentials", lambda number: True)
    calls = []

    def fake_create(number, to, subject, body, in_reply_to=None, thread_id=None):
        calls.append((number, to, subject, body, in_reply_to))
        return created or {"id": "draft_1", "message_id": "msg_1"}

    monkeypatch.setattr(gmail, "create_draft", fake_create)
    return calls


def test_draft_is_confirm_first_then_creates(monkeypatch):
    member = _member()
    calls = _enable_email(monkeypatch)

    msg = brain._tool_draft_email(
        member, {"to": "nan@example.com", "subject": "Sunday lunch", "body": "See you at 1!"}
    )
    assert "confirmation_id=" in msg
    assert calls == [], "draft must not be created before confirmation"

    pending = brain._pending_for(member.id)
    assert pending is not None and pending["kind"] == "email_draft"
    cid = pending["id"]

    out = brain._tool_resolve_confirmation(member, {"confirmation_id": cid, "approve": True})
    assert "Drafts" in out and "never send" in out.lower()
    assert len(calls) == 1
    number, to, subject, body, _ = calls[0]
    assert number == NUMBER and to == "nan@example.com" and subject == "Sunday lunch"

    from sqlalchemy import select

    with db.session_scope() as session:
        draft = session.scalars(select(db.EmailDraft)).first()
        pc = session.get(db.PendingConfirmation, cid)
        assert draft is not None and draft.gmail_draft_id == "draft_1"
        assert draft.recipient == "nan@example.com"
        assert pc.status == db.PC_EXECUTED


def test_decline_creates_no_draft(monkeypatch):
    member = _member()
    calls = _enable_email(monkeypatch)
    brain._tool_draft_email(member, {"to": "x@example.com", "subject": "Hi", "body": "Yo"})
    cid = brain._pending_for(member.id)["id"]

    out = brain._tool_resolve_confirmation(member, {"confirmation_id": cid, "approve": False})
    assert "cancelled" in out.lower()
    assert calls == []


def test_email_not_configured_message(monkeypatch):
    member = _member()
    monkeypatch.setattr(gmail, "has_credentials", lambda number: False)
    out = brain._tool_draft_email(member, {"to": "x@example.com", "subject": "Hi", "body": "Yo"})
    assert "isn't set up" in out


def test_gmail_client_has_no_send_capability():
    """Guardrail: the Gmail client must never call a send path (spec §9/§10).

    Parse the AST so we flag a real `.send(...)` call, not mentions of "send"
    in the docstring/comments.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gmail))
    send_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send"
    ]
    assert send_calls == [], "the Gmail client must never call .send()"
    assert not hasattr(gmail, "send")
