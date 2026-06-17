"""Tests for the Google Calendar supplemental-tool loader.

The loader must NEVER raise during agent startup — a missing/expired token
should degrade gracefully to "no calendar tools" so the rest of the agent
(search, email) keeps working. These tests pin that contract, including the
``invalid_grant`` (expired/revoked token) path that took calendar down.
"""
import sys
import types

import pytest

from app.agent import tools as tools_mod


def _settings(creds_file: str, token_file: str):
    return types.SimpleNamespace(
        google_credentials_file=creds_file,
        google_token_file=token_file,
    )


def test_returns_empty_when_creds_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_mod, "get_settings",
                        lambda: _settings("/no/such/creds.json", "/no/such/token.json"))
    assert tools_mod._get_calendar_tools() == []


def test_returns_empty_when_token_file_missing(monkeypatch, tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    monkeypatch.setattr(tools_mod, "get_settings",
                        lambda: _settings(str(creds), str(tmp_path / "token.json")))
    assert tools_mod._get_calendar_tools() == []


def _install_fake_lgc(monkeypatch, *, get_credentials, service=None):
    """Inject a fake langchain_google_community so we can test without the dep."""
    root = types.ModuleType("langchain_google_community")
    calendar = types.ModuleType("langchain_google_community.calendar")
    utils = types.ModuleType("langchain_google_community.calendar.utils")
    utils.get_google_credentials = get_credentials
    utils.build_calendar_service = lambda credentials=None: (service or object())

    monkeypatch.setitem(sys.modules, "langchain_google_community", root)
    monkeypatch.setitem(sys.modules, "langchain_google_community.calendar", calendar)
    monkeypatch.setitem(sys.modules, "langchain_google_community.calendar.utils", utils)


def _existing_paths(monkeypatch, tmp_path):
    creds = tmp_path / "credentials.json"
    token = tmp_path / "token.json"
    creds.write_text("{}")
    token.write_text("{}")
    monkeypatch.setattr(tools_mod, "get_settings",
                        lambda: _settings(str(creds), str(token)))


def test_invalid_grant_degrades_to_empty(monkeypatch, tmp_path, caplog):
    _existing_paths(monkeypatch, tmp_path)

    def _raise(**_):
        raise Exception("('invalid_grant: Bad Request', {'error': 'invalid_grant'})")

    _install_fake_lgc(monkeypatch, get_credentials=_raise)

    with caplog.at_level("WARNING"):
        result = tools_mod._get_calendar_tools()

    assert result == []
    assert any("invalid_grant" in r.message or "expired" in r.message.lower()
               for r in caplog.records)


def test_unexpected_error_degrades_to_empty(monkeypatch, tmp_path):
    _existing_paths(monkeypatch, tmp_path)
    _install_fake_lgc(monkeypatch,
                      get_credentials=lambda **_: (_ for _ in ()).throw(RuntimeError("nope")))
    assert tools_mod._get_calendar_tools() == []


def test_happy_path_returns_our_custom_tools(monkeypatch, tmp_path):
    _existing_paths(monkeypatch, tmp_path)
    _install_fake_lgc(monkeypatch, get_credentials=lambda **_: object())
    tools = tools_mod._get_calendar_tools()
    assert {t.name for t in tools} == {"list_calendar_events", "create_calendar_event"}


def test_list_events_formats_results(monkeypatch, tmp_path):
    _existing_paths(monkeypatch, tmp_path)

    class _FakeService:
        def events(self):
            return self

        def list(self, **kwargs):
            self._kwargs = kwargs
            return self

        def execute(self):
            return {"items": [
                {"start": {"dateTime": "2026-06-17T09:00:00-06:00"}, "summary": "Standup"},
                {"start": {"date": "2026-06-17"}, "summary": "All-day review"},
            ]}

    _install_fake_lgc(monkeypatch, get_credentials=lambda **_: object(), service=_FakeService())
    tools = {t.name: t for t in tools_mod._get_calendar_tools()}
    out = tools["list_calendar_events"].invoke(
        {"start": "2026-06-17T00:00:00", "end": "2026-06-17T23:59:59"}
    )
    assert "Standup" in out and "All-day review" in out


def test_list_events_empty(monkeypatch, tmp_path):
    _existing_paths(monkeypatch, tmp_path)

    class _Empty:
        def events(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"items": []}

    _install_fake_lgc(monkeypatch, get_credentials=lambda **_: object(), service=_Empty())
    tools = {t.name: t for t in tools_mod._get_calendar_tools()}
    out = tools["list_calendar_events"].invoke({"start": "2026-06-17T00:00:00", "end": "2026-06-17T23:59:59"})
    assert "No events found" in out
