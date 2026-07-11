"""Tests for WAHA recipient addressing (phone vs. privacy-id chats)."""
import os
import tempfile

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALLOWED_NUMBERS", "447700900000")
os.environ.setdefault("TZ", "Europe/London")
_TMP = tempfile.mkdtemp(prefix="spotbot-wa-test-")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "app.sqlite"))

import whatsapp  # noqa: E402


def test_bare_number_gets_cus_suffix():
    assert whatsapp._chat_id("447700900000") == "447700900000@c.us"


def test_full_chat_ids_pass_through():
    # A phone chat and a privacy-id (LID) chat must be addressed verbatim.
    assert whatsapp._chat_id("447700900000@c.us") == "447700900000@c.us"
    assert whatsapp._chat_id("22037762453629@lid") == "22037762453629@lid"
