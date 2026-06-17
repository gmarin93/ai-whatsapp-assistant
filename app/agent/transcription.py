"""Speech-to-text for WhatsApp voice notes (OpenAI Whisper).

WhatsApp voice notes arrive as Opus-in-Ogg (``audio/ogg``), which Whisper
accepts directly — no transcoding needed. We just need to give the upload a
filename with the right extension so the API can sniff the format.
"""
import logging
from io import BytesIO

from app.config import get_settings

logger = logging.getLogger(__name__)

# Map common WhatsApp audio mime types to a file extension Whisper recognises.
_EXT_BY_MIME = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "audio/wav": "wav",
    "audio/webm": "webm",
}


def _extension_for(mime_type: str) -> str:
    return _EXT_BY_MIME.get(mime_type.split(";")[0].strip().lower(), "ogg")


async def transcribe_audio(audio: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe raw audio bytes to text. Returns "" if nothing was recognised."""
    from openai import AsyncOpenAI

    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key or None)

    buffer = BytesIO(audio)
    buffer.name = f"voice.{_extension_for(mime_type)}"  # Whisper sniffs by name

    result = await client.audio.transcriptions.create(
        model=settings.whisper_model,
        file=buffer,
    )
    text = (getattr(result, "text", "") or "").strip()
    logger.info("Transcribed %d bytes (%s) → %d chars", len(audio), mime_type, len(text))
    return text
