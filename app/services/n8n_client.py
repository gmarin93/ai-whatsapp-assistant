
import logging

import httpx

logger = logging.getLogger(__name__)


class N8NClientError(Exception):
    """Raised when the n8n integration cannot return a valid reply."""


class N8NClient:
    """Send messages to n8n without interacting with the WhatsApp API."""

    def __init__(
        self,
        webhook_url: str,
        api_key: str,
        timeout: float = 30.0,
    ) -> None:
        if not webhook_url or not webhook_url.startswith(("http://", "https://")):
            raise ValueError("A valid n8n webhook URL is required")

        if not api_key:
            raise ValueError("An n8n API key is required")

        if timeout <= 0:
            raise ValueError("Timeout must be greater than zero")

        self.webhook_url = webhook_url
        self.api_key = api_key
        self.timeout = timeout

    async def generate_reply(
        self,
        message_id: str,
        wa_id: str,
        text: str,
    ) -> str:
        """Request an AI reply from n8n without sending it to WhatsApp."""

        if not message_id or not wa_id or not text.strip():
            raise ValueError("Message ID, WhatsApp ID and text are required")

        payload = {
            "message_id": message_id,
            "wa_id": wa_id,
            "text": text,
        }

        headers = {
            "X-N8N-API-KEY": self.api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.webhook_url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()

            data = response.json()

            if not isinstance(data, dict):
                raise N8NClientError("Invalid n8n response format")

            reply = data.get("reply")

            if not isinstance(reply, str) or not reply.strip():
                raise N8NClientError("n8n returned an empty or invalid reply")

            return reply.strip()

        except httpx.TimeoutException as exc:
            logger.error("n8n request timed out")
            raise N8NClientError("n8n request timed out") from exc

        except httpx.HTTPError as exc:
            logger.error("n8n HTTP request failed: %s", type(exc).__name__)
            raise N8NClientError("n8n HTTP request failed") from exc

        except ValueError as exc:
            if isinstance(exc, N8NClientError):
                raise
            logger.error("n8n returned invalid JSON")
            raise N8NClientError("n8n returned invalid JSON") from exc
