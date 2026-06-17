import logging
from typing import TYPE_CHECKING

import httpx

from app.agent.graph import WhatsAppAgent
from app.config import get_settings
from app.meta.client import WhatsAppClient

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# Fake sender in Meta's "Enviar al servidor" sample payload — cannot receive replies.
META_SAMPLE_WA_ID = "16315551181"


def _error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.text
    return str(exc)


class MessageHandler:
    """Connects incoming WhatsApp text → LangGraph agent → outbound reply.

    Pass ``tools`` to inject MCP-backed (or any LangChain) tools into the
    agent at startup rather than letting the agent discover them internally.
    When ``tools`` is omitted the agent falls back to its own tool loading.
    """

    def __init__(self, tools: list["BaseTool"] | None = None) -> None:
        self.agent = WhatsAppAgent(tools=tools)
        self.agent.build()
        self.whatsapp = WhatsAppClient()

    def _is_allowed(self, wa_id: str) -> bool:
        settings = get_settings()
        if not settings.allowed_wa_ids.strip():
            return True
        allowed = {x.strip() for x in settings.allowed_wa_ids.split(",") if x.strip()}
        return wa_id in allowed

    async def handle_audio(self, wa_id: str, media_id: str) -> None:
        """Download a voice note, transcribe it, then process it like text."""
        if not self._is_allowed(wa_id):
            logger.warning("Ignored voice note from non-allowed wa_id=%s", wa_id)
            return

        try:
            audio, mime = await self.whatsapp.download_media(media_id)
            from app.agent.transcription import transcribe_audio

            text = await transcribe_audio(audio, mime)
        except Exception:
            logger.exception("Failed to transcribe voice note from wa_id=%s", wa_id)
            await self.whatsapp.send_text(
                wa_id,
                "Sorry, I couldn't process that voice note. Please try again or type your message.",
            )
            return

        if not text:
            await self.whatsapp.send_text(
                wa_id, "I couldn't hear anything in that voice note — mind trying again?"
            )
            return

        logger.info("Transcribed voice note from wa_id=%s (%d chars)", wa_id, len(text))
        # Show the user what we heard (nice confirmation), then handle it as text.
        await self.whatsapp.send_text(wa_id, f"🎤 _I heard:_ “{text}”")
        await self.handle_text(wa_id, text)

    async def handle_text(self, wa_id: str, text: str) -> None:
        if not self._is_allowed(wa_id):
            logger.warning("Ignored message from non-allowed wa_id=%s", wa_id)
            return

        text = (text or "").strip()
        if not text:
            return

        if text.lower() == "/reset":
            await self.agent.reset_thread(wa_id)
            await self.whatsapp.send_text(wa_id, "Conversation reset.")
            return

        # Greetings / /menu / chat-open welcome → show the full capabilities menu.
        from app.agent.menu import (
            MENU_BODY,
            MENU_BUTTON_TEXT,
            MENU_ROWS,
            MENU_SECTION_TITLE,
            is_menu_trigger,
        )

        if is_menu_trigger(text):
            await self.whatsapp.send_list(
                wa_id, MENU_BODY, MENU_BUTTON_TEXT, MENU_ROWS, MENU_SECTION_TITLE
            )
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
