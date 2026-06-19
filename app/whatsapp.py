"""Thin async client for WAHA (WhatsApp HTTP API).

Used both from the request path (replying to an inbound message) and from the
scheduler's fire callback (proactively sending a due reminder). It holds no
request-scoped state, so it is safe to call from either context.
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.waha_api_key:
        headers["X-Api-Key"] = settings.waha_api_key
    return headers


async def send_text(chat_id: str, text: str) -> None:
    """Send a text message to a WhatsApp chat via WAHA.

    `chat_id` is WAHA's chat identifier, e.g. "447911123456@c.us".
    """
    url = f"{settings.waha_base_url}/api/sendText"
    payload = {"session": settings.waha_session, "chatId": chat_id, "text": text}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
    log.info("Sent WhatsApp message to %s (%d chars)", chat_id, len(text))


def to_chat_id(number: str) -> str:
    """Turn a bare phone number into a WAHA chat id."""
    if "@" in number:
        return number
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"{digits}@c.us"


def parse_inbound(body: dict) -> tuple[str, str] | None:
    """Extract (chat_id, text) from a WAHA webhook payload.

    Returns None for events we ignore (non-message events, our own outbound
    messages, or messages without text).
    """
    if body.get("event") != "message":
        return None
    payload = body.get("payload") or {}
    if payload.get("fromMe"):
        return None
    chat_id = payload.get("from")
    text = payload.get("body")
    if not chat_id or not text:
        return None
    return chat_id, text
