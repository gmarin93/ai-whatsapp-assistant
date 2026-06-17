"""Tests for the web-search MCP tool (Serper primary, DuckDuckGo fallback)."""
import asyncio
import sys
import types

import pytest

from app.mcp import search_server


# ── _format_serper ────────────────────────────────────────────────────────────

def test_format_serper_prefers_answer_box():
    data = {
        "answerBox": {"answer": "42"},
        "organic": [{"title": "T", "snippet": "S", "link": "http://x"}],
    }
    out = search_server._format_serper(data)
    assert out.startswith("Answer: 42")
    assert "- T: S (http://x)" in out


def test_format_serper_organic_only():
    data = {"organic": [{"title": "T", "snippet": "S", "link": "http://x"}]}
    assert search_server._format_serper(data) == "- T: S (http://x)"


# ── search_web: Serper path (httpx mocked) ────────────────────────────────────

def _install_fake_httpx(monkeypatch, *, json_data=None, raise_exc=None):
    mod = types.ModuleType("httpx")

    class _Resp:
        def json(self):
            return json_data

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            if raise_exc:
                raise raise_exc
            return _Resp()

    mod.AsyncClient = _Client
    monkeypatch.setitem(sys.modules, "httpx", mod)


def test_search_uses_serper_when_key_present(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "serper-test")
    _install_fake_httpx(monkeypatch, json_data={
        "organic": [{"title": "Py", "snippet": "lang", "link": "http://py"}]
    })
    result = asyncio.run(search_server.search_web("python"))
    assert result == "- Py: lang (http://py)"


def test_serper_failure_falls_back_to_ddg(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "serper-test")
    _install_fake_httpx(monkeypatch, raise_exc=RuntimeError("serper down"))
    monkeypatch.setattr(search_server, "_ddg_search", lambda q: "ddg result")
    assert asyncio.run(search_server.search_web("x")) == "ddg result"


def test_no_key_uses_ddg(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setattr(search_server, "_ddg_search", lambda q: "ddg only")
    assert asyncio.run(search_server.search_web("x")) == "ddg only"


def test_total_failure_returns_graceful_message(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    def _boom(_q):
        raise RuntimeError("no network")

    monkeypatch.setattr(search_server, "_ddg_search", _boom)
    result = asyncio.run(search_server.search_web("weather"))
    assert "unavailable" in result.lower()
    assert "weather" in result
