"""Gmail client — DRAFT CREATION ONLY (M4).

Consumer Gmail can't be reached by a service account, so email is per-user OAuth
and opt-in (spec §9): a member is email-enabled only if a token file exists for
their number under config.GMAIL_TOKENS_DIR. Tokens are produced out-of-band by
scripts/gmail_authorize.py and mounted read-only; we never write them back
(a refresh only mints a short-lived access token in memory).

HARD RULE (spec §9/§10): this bot never sends email. There is deliberately NO
call to users().messages().send() or users().drafts().send() anywhere in this
codebase, and none must ever be added. We only create drafts for a human to
review and send themselves.

The Google libraries are imported lazily so the app runs fine (email simply
disabled) when they're absent, and so the test suite can stub these functions.
"""
import base64
import os
from email.mime.text import MIMEText

import config

# gmail.compose is the narrowest scope that permits draft management; we use it
# strictly for drafts().create and never for any send operation.
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def _token_path(number: str) -> str:
    return os.path.join(config.GMAIL_TOKENS_DIR, f"{number}.json")


def has_credentials(number: str) -> bool:
    """True if this member has opted into email (a token file exists)."""
    return bool(config.GMAIL_TOKENS_DIR) and os.path.exists(_token_path(number))


def _service(number: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(_token_path(number), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())  # in-memory only; token dir is read-only
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def create_draft(number, to, subject, body, in_reply_to=None, thread_id=None) -> dict:
    """Create a Gmail draft in the member's own mailbox. Returns {id, message_id}.

    Never sends — leaves the draft for the human to review and send.
    """
    mime = MIMEText(body)
    mime["to"] = to
    mime["subject"] = subject
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    message = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id

    draft = (
        _service(number)
        .users()
        .drafts()
        .create(userId="me", body={"message": message})
        .execute()
    )
    return {"id": draft["id"], "message_id": draft.get("message", {}).get("id")}
