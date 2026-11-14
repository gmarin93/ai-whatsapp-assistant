"""Tests for voice-note transcription: webhook parsing, Whisper call, handler flow."""
import asyncio
import sys
import types

import pytest

from app.agent import transcription
from app.services.handler import MessageHandler
from app.webhooks.whatsapp import _extract_messages, _parse_message


# ── webhook parses audio messages into an audio_id job ────────────────────────

def test_parse_audio_message():
    msg = {"id": "wamid.9", "from": "50660050800", "type": "audio",
           "audio": {"id": "MEDIA123", "mime_type": "audio/ogg; codecs=opus", "voice": True}}
    assert _parse_message(msg) == {"id": "wamid.9", "wa_id": "50660050800", "audio_id": "MEDIA123"}


def test_parse_audio_without_id_is_dropped():
    msg = {"id": "x", "from": "123", "type": "audio", "audio": {}}
    assert _parse_message(msg) is None


def test_extract_messages_routes_audio():   
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.9", "from": "123", "type": "audio", "audio": {"id": "M1"}}
    ]}}]}]}
    assert _extract_messages(payload) == [
        {
            "id": "wamid.9",
            "wa_id": "123",
            "audio_id": "M1",
            "phone_number_id": "",
        }
    ]


# ── transcription: correct filename + returns text (OpenAI mocked) ────────────

def test_extension_for_mime():
    assert transcription._extension_for("audio/ogg; codecs=opus") == "ogg"
    assert transcription._extension_for("audio/mpeg") == "mp3"
    assert transcription._extension_for("audio/weird") == "ogg"  # safe default


def _install_fake_openai(monkeypatch, capture):
    mod = types.ModuleType("openai")

    class _AsyncOpenAI:
        def __init__(self, **kwargs):
            capture["init"] = kwargs

            async def _create(model, file):
                capture["model"] = model
                capture["filename"] = file.name
                capture["bytes"] = file.getvalue()
                return types.SimpleNamespace(text="  hello world  ")

            self.audio = types.SimpleNamespace(
                transcriptions=types.SimpleNamespace(create=_create)
            )

    mod.AsyncOpenAI = _AsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)


def test_transcribe_audio_names_file_and_strips(monkeypatch):
    capture = {}
    _install_fake_openai(monkeypatch, capture)

    out = asyncio.run(transcription.transcribe_audio(b"RAWAUDIO", "audio/ogg; codecs=opus"))

    assert out == "hello world"
    assert capture["filename"] == "voice.ogg"
    assert capture["bytes"] == b"RAWAUDIO"


# ── handler.handle_audio flow ─────────────────────────────────────────────────

class _FakeWhatsApp:
    def __init__(self, audio=(b"AUDIO", "audio/ogg")):
        self.sent = []
        self._audio = audio

    async def download_media(self, media_id):
        self._downloaded = media_id
        return self._audio

    async def send_text(self, wa_id, body):
        self.sent.append((wa_id, body))


def _bare_handler(whatsapp):
    h = MessageHandler.__new__(MessageHandler)
    h.whatsapp = whatsapp
    return h


def test_handle_audio_transcribes_then_handles_text(monkeypatch):
    wa = _FakeWhatsApp()
    h = _bare_handler(wa)

    monkeypatch.setattr(h, "_is_allowed", lambda wa_id: True)
    handled = {}

    async def _fake_transcribe(audio, mime):
        return "what's on my calendar today?"

    monkeypatch.setattr("app.agent.transcription.transcribe_audio", _fake_transcribe)

    async def _fake_handle_text(wa_id, text):
        handled["text"] = text

    monkeypatch.setattr(h, "handle_text", _fake_handle_text)

    asyncio.run(h.handle_audio("123", "MEDIA1"))

    assert wa._downloaded == "MEDIA1"
    assert handled["text"] == "what's on my calendar today?"
    # User gets a confirmation of what was heard.
    assert any("I heard" in body for _wa, body in wa.sent)


def test_handle_audio_empty_transcript_asks_to_retry(monkeypatch):
    wa = _FakeWhatsApp()
    h = _bare_handler(wa)
    monkeypatch.setattr(h, "_is_allowed", lambda wa_id: True)

    async def _empty(audio, mime):
        return ""

    monkeypatch.setattr("app.agent.transcription.transcribe_audio", _empty)

    called = {"text": False}

    async def _fake_handle_text(wa_id, text):
        called["text"] = True

    monkeypatch.setattr(h, "handle_text", _fake_handle_text)

    asyncio.run(h.handle_audio("123", "MEDIA1"))

    assert called["text"] is False
    assert any("couldn't hear" in body for _wa, body in wa.sent)


def test_handle_audio_download_error_is_graceful(monkeypatch):
    class _Boom(_FakeWhatsApp):
        async def download_media(self, media_id):
            raise RuntimeError("media gone")

    wa = _Boom()
    h = _bare_handler(wa)
    monkeypatch.setattr(h, "_is_allowed", lambda wa_id: True)

    asyncio.run(h.handle_audio("123", "MEDIA1"))

    assert any("couldn't process" in body for _wa, body in wa.sent)
