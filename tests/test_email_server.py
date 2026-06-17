"""Tests for the Resend email MCP tool.

These pin the error-surfacing behaviour (Resend's ``code``/``message`` must
reach the agent, not a bare repr), the config guards, and the success path.
"""
import asyncio
import sys
import types

import pytest

from app.mcp import email_server


class _FakeResendError(Exception):
    """Mimics resend.exceptions.ResendError (code + message)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ── _describe_resend_error ────────────────────────────────────────────────────

def test_describe_combines_code_and_message():
    exc = _FakeResendError(403, "The gmail.com domain is not verified")
    assert email_server._describe_resend_error(exc) == "403: The gmail.com domain is not verified"


def test_describe_message_only_when_no_code():
    assert email_server._describe_resend_error(ValueError("boom")) == "boom"


# ── send_email config guards ──────────────────────────────────────────────────

def test_send_email_skipped_without_api_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    result = asyncio.run(email_server.send_email("subj", "body"))
    assert result == "skipped: RESEND_API_KEY not configured"


def test_send_email_skipped_without_from(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
    result = asyncio.run(email_server.send_email("subj", "body"))
    assert result == "skipped: RESEND_FROM_EMAIL not configured"


# ── send_email send paths (Resend mocked) ─────────────────────────────────────

def _install_fake_resend(monkeypatch, *, send):
    """Inject a fake ``resend`` module whose Emails.send runs *send*."""
    mod = types.ModuleType("resend")
    mod.api_key = None
    mod.Emails = types.SimpleNamespace(send=lambda params: send(params))
    monkeypatch.setitem(sys.modules, "resend", mod)


def test_send_email_success(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("RESEND_TO_EMAIL", "to@example.com")

    captured = {}

    def _send(params):
        captured.update(params)
        return {"id": "abc-123"}

    _install_fake_resend(monkeypatch, send=_send)

    result = asyncio.run(email_server.send_email("hi", "there"))
    assert result == "sent (id abc-123)"
    # The agent's recipient/sender must be wired through to Resend correctly.
    assert captured["from"] == "from@example.com"
    assert captured["to"] == ["to@example.com"]
    assert captured["subject"] == "hi"


def test_send_email_to_defaults_to_from(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "solo@example.com")
    monkeypatch.delenv("RESEND_TO_EMAIL", raising=False)

    captured = {}
    _install_fake_resend(monkeypatch, send=lambda p: captured.update(p) or {"id": "x"})

    asyncio.run(email_server.send_email("hi", "there"))
    assert captured["to"] == ["solo@example.com"]


def test_send_email_unverified_domain_is_actionable(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "me@gmail.com")

    def _raise(_params):
        raise _FakeResendError(403, "The gmail.com domain is not verified")

    _install_fake_resend(monkeypatch, send=_raise)

    result = asyncio.run(email_server.send_email("hi", "there"))
    assert "not verified" in result
    assert "onboarding@resend.dev" in result  # points the user at the fix


def test_send_email_other_error_surfaces_detail(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "from@example.com")

    def _raise(_params):
        raise _FakeResendError(429, "Too many requests")

    _install_fake_resend(monkeypatch, send=_raise)

    result = asyncio.run(email_server.send_email("hi", "there"))
    assert result == "error: 429: Too many requests"
