import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppClient:
    """Send messages via Meta WhatsApp Cloud API."""

    def __init__(self) -> None:
        settings = get_settings()
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.token = settings.whatsapp_access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def send_text(self, to_wa_id: str, body: str) -> dict:
        """Send a text message. Truncates to WhatsApp limit (~4096 chars)."""
        text = body[:4096] if body else "(empty)"
        url = f"{GRAPH_API_BASE}/{self.phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to_wa_id,
            "type": "text",
            "text": {"body": text},
        }
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

    # TODO: mark_read(message_id), send_template(...), send_media(...)
