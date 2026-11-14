"""Tests for interactive (button/list) messages and tap handling."""
import pytest

from app.agent import menu
from app.meta import client
from app.webhooks.whatsapp import _extract_messages, _message_text


# ── payload builders ──────────────────────────────────────────────────────────

def test_build_buttons_payload_shape_and_limits():
    long_title = "x" * 50
    payload = client.build_buttons_payload(
        "123", "Pick one", [("a", "Apple"), ("b", "Banana"), ("c", "Cherry"), ("d", "Date")]
    )
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "button"
    btns = payload["interactive"]["action"]["buttons"]
    assert len(btns) == 3  # capped at MAX_BUTTONS, 4th dropped
    assert btns[0] == {"type": "reply", "reply": {"id": "a", "title": "Apple"}}

    capped = client.build_buttons_payload("123", "b", [("a", long_title)])
    assert len(capped["interactive"]["action"]["buttons"][0]["reply"]["title"]) == client.BTN_TITLE_MAX


def test_build_list_payload_shape():
    payload = client.build_list_payload(
        "123", "Menu", "Open", [("r1", "Row one", "desc one")], section_title="Sec"
    )
    inter = payload["interactive"]
    assert inter["type"] == "list"
    assert inter["action"]["button"] == "Open"
    row = inter["action"]["sections"][0]["rows"][0]
    assert row == {"id": "r1", "title": "Row one", "description": "desc one"}


# ── menu tap expansion ────────────────────────────────────────────────────────

def test_every_menu_row_has_an_expansion():
    for rid, _title, _desc in menu.MENU_ROWS:
        assert rid in menu.MENU_EXPANSIONS


def test_menu_covers_all_core_tools():
    ids = {rid for rid, _t, _d in menu.MENU_ROWS}
    assert {"menu_search", "menu_email", "menu_agenda", "menu_reminder", "menu_briefing"} <= ids


def test_expand_tap_falls_back_to_title():
    assert menu.expand_tap("unknown_id", "Some Title") == "Some Title"
    assert menu.expand_tap("menu_agenda", "📅 Today's agenda") == menu.MENU_EXPANSIONS["menu_agenda"]


def test_is_menu_trigger():
    assert menu.is_menu_trigger("hi")
    assert menu.is_menu_trigger("  Hola ")
    assert menu.is_menu_trigger("/menu")
    assert not menu.is_menu_trigger("send an email to bob")


# ── webhook parsing of taps ───────────────────────────────────────────────────

def test_message_text_from_button_reply_expands():
    msg = {
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "menu_agenda", "title": "📅 Today's agenda"}},
    }
    assert _message_text(msg) == menu.MENU_EXPANSIONS["menu_agenda"]


def test_message_text_from_list_reply_unknown_id_uses_title():
    msg = {
        "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": "x", "title": "Custom"}},
    }
    assert _message_text(msg) == "Custom"


def test_message_text_plain_text():
    assert _message_text({"type": "text", "text": {"body": "hello"}}) == "hello"


def test_message_text_ignores_unsupported_types():
    assert _message_text({"type": "image", "image": {"id": "1"}}) == ""


def test_request_welcome_routes_to_menu():
    assert _message_text({"type": "request_welcome"}) == "/menu"


def test_extract_messages_handles_tap_payload():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.1",
                                    "from": "50660050800",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {"id": "menu_reminder", "title": "⏰ Set a reminder"},
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    out = _extract_messages(payload)
    assert out == [
        {
            "id": "wamid.1",
            "wa_id": "50660050800",
            "text": menu.MENU_EXPANSIONS["menu_reminder"],
            "phone_number_id": "",
        }
    ]
