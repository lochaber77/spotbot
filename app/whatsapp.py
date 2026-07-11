"""Thin client for sending WhatsApp messages via WAHA."""
import httpx

import config


def _chat_id(number: str) -> str:
    """WAHA chat id for a recipient.

    If we were handed a full chat id already (it contains '@', e.g. a
    '…@c.us' phone chat or a '…@lid' privacy-id chat), send to it verbatim.
    Otherwise treat it as a bare phone number and address it as '<number>@c.us'.
    """
    if "@" in number:
        return number
    return f"{number}@c.us"


async def send_text(number: str, text: str) -> None:
    headers = {}
    if config.WAHA_API_KEY:
        headers["X-Api-Key"] = config.WAHA_API_KEY

    payload = {
        "session": config.WAHA_SESSION,
        "chatId": _chat_id(number),
        "text": text,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{config.WAHA_URL}/api/sendText", json=payload, headers=headers
        )
        resp.raise_for_status()
