import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

# WhatsApp interactive-message limits (Meta Cloud API).
BTN_TITLE_MAX = 20
ROW_TITLE_MAX = 24
ROW_DESC_MAX = 72
MAX_BUTTONS = 3
MAX_ROWS = 10


def build_text_payload(to_wa_id: str, body: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": (body[:4096] if body else "(empty)")},
    }


def build_buttons_payload(to_wa_id: str, body: str, buttons: list[tuple[str, str]]) -> dict:
    """Up to 3 tappable reply buttons. ``buttons`` = list of (id, title)."""
    return {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": title[:BTN_TITLE_MAX]}}
                    for bid, title in buttons[:MAX_BUTTONS]
                ]
            },
        },
    }


def build_list_payload(
    to_wa_id: str,
    body: str,
    button_text: str,
    rows: list[tuple[str, str, str]],
    section_title: str = "Options",
) -> dict:
    """A tappable list menu. ``rows`` = list of (id, title, description)."""
    return {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {
                "button": button_text[:BTN_TITLE_MAX],
                "sections": [
                    {
                        "title": section_title[:24],
                        "rows": [
                            {
                                "id": rid,
                                "title": title[:ROW_TITLE_MAX],
                                "description": desc[:ROW_DESC_MAX],
                            }
                            for rid, title, desc in rows[:MAX_ROWS]
                        ],
                    }
                ],
            },
        },
    }


class WhatsAppClient:
    """Send messages via Meta WhatsApp Cloud API."""

    def __init__(self) -> None:
        settings = get_settings()
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.token = settings.whatsapp_access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _send(self, payload: dict) -> dict:
        url = f"{GRAPH_API_BASE}/{self.phone_number_id}/messages"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=self._headers())
            if response.is_error:
                logger.error(
                    "WhatsApp Graph API error %s: %s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            return response.json()

    async def send_text(self, to_wa_id: str, body: str) -> dict:
        """Send a text message. Truncates to WhatsApp limit (~4096 chars)."""
        return await self._send(build_text_payload(to_wa_id, body))

    async def send_buttons(
        self, to_wa_id: str, body: str, buttons: list[tuple[str, str]]
    ) -> dict:
        """Send up to 3 tappable reply buttons. ``buttons`` = list of (id, title)."""
        return await self._send(build_buttons_payload(to_wa_id, body, buttons))

    async def send_list(
        self,
        to_wa_id: str,
        body: str,
        button_text: str,
        rows: list[tuple[str, str, str]],
        section_title: str = "Options",
    ) -> dict:
        """Send a tappable list menu. ``rows`` = list of (id, title, description)."""
        return await self._send(
            build_list_payload(to_wa_id, body, button_text, rows, section_title)
        )

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Download a media object (e.g. a voice note) by its media id.

        Two-step Graph API flow: resolve the media id to a short-lived URL, then
        fetch the bytes (both calls need the bearer token). Returns
        ``(content, mime_type)``.
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            meta = await client.get(
                f"{GRAPH_API_BASE}/{media_id}", headers=self._headers()
            )
            meta.raise_for_status()
            media_url = meta.json()["url"]

            blob = await client.get(media_url, headers=self._headers())
            blob.raise_for_status()
            mime = blob.headers.get("content-type", "audio/ogg").split(";")[0]
            return blob.content, mime

    # TODO: mark_read(message_id), send_template(...), send_media(...)
