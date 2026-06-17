"""Tests for conversation auto-recovery from a corrupted thread."""
import asyncio
import types

import pytest

from app.agent import graph
from app.agent.graph import WhatsAppAgent, _is_broken_history

_BROKEN_MSG = (
    "Error code: 400 - An assistant message with 'tool_calls' must be followed by "
    "tool messages responding to each 'tool_call_id'. The following tool_call_ids "
    "did not have response messages: call_abc"
)


def test_is_broken_history_detects_openai_error():
    assert _is_broken_history(Exception(_BROKEN_MSG))


def test_is_broken_history_ignores_unrelated_errors():
    assert not _is_broken_history(Exception("Connection reset by peer"))
    assert not _is_broken_history(ValueError("bad input"))


class _FakeGraph:
    def __init__(self, fail_first: bool):
        self.calls = 0
        self.thread_ids: list[str] = []
        self._fail_first = fail_first

    async def ainvoke(self, payload, config):
        self.calls += 1
        self.thread_ids.append(config["configurable"]["thread_id"])
        if self._fail_first and self.calls == 1:
            raise Exception(_BROKEN_MSG)
        return {"messages": [types.SimpleNamespace(content="ok")]}


class _FakeCheckpointer:
    def __init__(self, delete_ok: bool = True):
        self.deleted: list[str] = []
        self._delete_ok = delete_ok

    async def adelete_thread(self, thread_id):
        if not self._delete_ok:
            raise RuntimeError("redis down")
        self.deleted.append(thread_id)


def _bare_agent(graph_obj, checkpointer=None):
    """Build an agent without running __init__ (avoids constructing the LLM)."""
    agent = WhatsAppAgent.__new__(WhatsAppAgent)
    agent.graph = graph_obj
    agent.checkpointer = checkpointer or _FakeCheckpointer()
    agent._checkpointer_ready = True
    agent._setup_lock = asyncio.Lock()
    return agent


def test_reply_resets_and_retries_on_broken_history():
    g = _FakeGraph(fail_first=True)
    cp = _FakeCheckpointer()
    agent = _bare_agent(g, cp)

    out = asyncio.run(agent.reply("123", "what's on my calendar?"))

    assert out == "ok"
    assert cp.deleted == ["123"]   # corrupted thread was wiped via the checkpointer
    assert g.calls == 2            # failed once, retried once
    assert g.thread_ids == ["123", "123"]  # retried on the same (now-clean) thread


def test_reply_retries_on_fresh_thread_when_delete_fails():
    g = _FakeGraph(fail_first=True)
    cp = _FakeCheckpointer(delete_ok=False)
    agent = _bare_agent(g, cp)

    out = asyncio.run(agent.reply("123", "hi"))

    assert out == "ok"
    assert g.calls == 2
    # Delete failed → retry used a fresh, distinct thread id so a reply still goes out.
    assert g.thread_ids[0] == "123"
    assert g.thread_ids[1].startswith("123:r") and g.thread_ids[1] != "123"


def test_reply_does_not_reset_on_normal_run():
    g = _FakeGraph(fail_first=False)
    cp = _FakeCheckpointer()
    agent = _bare_agent(g, cp)

    out = asyncio.run(agent.reply("123", "hello"))

    assert out == "ok"
    assert cp.deleted == []
    assert g.calls == 1


def test_reply_reraises_unrelated_errors():
    class _Boom:
        async def ainvoke(self, payload, config):
            raise RuntimeError("network down")

    cp = _FakeCheckpointer()
    agent = _bare_agent(_Boom(), cp)
    with pytest.raises(RuntimeError, match="network down"):
        asyncio.run(agent.reply("123", "hi"))
    assert cp.deleted == []  # no reset attempted on unrelated errors
