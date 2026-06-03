import logging

import httpx

from app.agent.graph import WhatsAppAgent
from app.config import get_settings
from app.meta.client import WhatsAppClient

logger = logging.getLogger(__name__)

# Fake sender in Meta's "Enviar al servidor" sample payload — cannot receive replies.
META_SAMPLE_WA_ID = "16315551181"


def _error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.text
    return str(exc)


class MessageHandler:
    """Connects incoming WhatsApp text → LangGraph → outbound reply."""

    def __init__(self) -> None:
        self.agent = WhatsAppAgent()
        self.agent.build()
        self.whatsapp = WhatsAppClient()

    def _is_allowed(self, wa_id: str) -> bool:
        settings = get_settings()
        if not settings.allowed_wa_ids.strip():
            return True
        allowed = {x.strip() for x in settings.allowed_wa_ids.split(",") if x.strip()}
        return wa_id in allowed

    async def handle_text(self, wa_id: str, text: str) -> None:
        if not self._is_allowed(wa_id):
            logger.warning("Ignored message from non-allowed wa_id=%s", wa_id)
            return

        text = (text or "").strip()
        if not text:
            return

        # TODO: handle /reset, /help commands before calling the agent
        if text.lower() == "/reset":
            await self.agent.reset_thread(wa_id)
            await self.whatsapp.send_text(wa_id, "Conversation reset.")
            return

        try:
            logger.info("Processing message from wa_id=%s", wa_id)
            reply = await self.agent.reply(wa_id, text)
            logger.info("Sending reply to wa_id=%s (%d chars)", wa_id, len(reply))

            if wa_id == META_SAMPLE_WA_ID:
                logger.info(
                    "Meta sample test OK — AI reply generated. "
                    "Skipping send (16315551181 is a fake number). "
                    "Message your phone from WhatsApp → Meta test business number for a real reply."
                )
                return

            await self.whatsapp.send_text(wa_id, reply)
        except Exception as exc:
            err = _error_detail(exc)
            if "131030" in err or "not in allowed list" in err:
                logger.warning(
                    "Cannot reply to wa_id=%s — add this number in Meta → Paso 1 → "
                    "test recipient list, or use your real phone to message the test business number.",
                    wa_id,
                )
                return
            logger.exception("Failed to handle message from wa_id=%s", wa_id)
            if "401" in err or "401" in str(exc):
                logger.error(
                    "WhatsApp access token rejected (401). "
                    "Generate a new token in Meta → Paso 1 → API Setup, "
                    "update WHATSAPP_ACCESS_TOKEN in .env, restart uvicorn."
                )
                return
            try:
                await self.whatsapp.send_text(
                    wa_id,
                    "Sorry, something went wrong. Please try again later.",
                )
            except Exception:
                logger.exception("Could not send error message to wa_id=%s", wa_id)
