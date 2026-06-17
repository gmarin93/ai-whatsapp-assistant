"""Tests for the daily briefing builder + delivery."""
import asyncio

import pytest

from app.agent import briefing


def test_prompt_covers_all_three_sections():
    prompt = briefing.build_briefing_prompt()
    low = prompt.lower()
    assert "calendar" in low
    assert "world cup" in low
    assert "weather" in low
    assert "email" in low  # explicitly tells the agent NOT to email


class _FakeAgent:
    def __init__(self, fail_for=None):
        self.fail_for = fail_for or set()
        self.calls = []

    async def reply(self, wa_id, text, thread_id=None):
        self.calls.append((wa_id, thread_id))
        if wa_id in self.fail_for:
            raise RuntimeError("agent boom")
        return f"briefing for {wa_id}"


class _FakeWhatsApp:
    def __init__(self):
        self.sent = []

    async def send_text(self, wa_id, body):
        self.sent.append((wa_id, body))


class _FakeHandler:
    def __init__(self, fail_for=None):
        self.agent = _FakeAgent(fail_for)
        self.whatsapp = _FakeWhatsApp()


def test_run_briefing_sends_to_each_user():
    handler = _FakeHandler()
    asyncio.run(briefing.run_briefing(handler, ["111", "222"]))

    assert handler.whatsapp.sent == [("111", "briefing for 111"), ("222", "briefing for 222")]
    # Uses an isolated thread per user so the chat history isn't polluted.
    assert handler.agent.calls == [("111", "briefing:111"), ("222", "briefing:222")]


def test_run_briefing_continues_after_one_failure():
    handler = _FakeHandler(fail_for={"111"})
    asyncio.run(briefing.run_briefing(handler, ["111", "222"]))

    # 111 failed before send; 222 still delivered.
    assert handler.whatsapp.sent == [("222", "briefing for 222")]
