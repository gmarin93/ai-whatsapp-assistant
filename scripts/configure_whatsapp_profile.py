"""Configure WhatsApp 'conversational components' so users see options on open.

Sets, on your phone number, via the Graph API:
  * welcome message  — fires a `request_welcome` webhook the first time a user
    opens the chat (our webhook maps that to the menu);
  * ice breakers     — tappable conversation starters shown on an empty chat;
  * commands         — `/command` autocomplete in the WhatsApp composer.

Run once (re-run to update):

    docker compose exec worker python scripts/configure_whatsapp_profile.py
    # or locally:
    python scripts/configure_whatsapp_profile.py

Docs: WhatsApp Cloud API → Conversational Components.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.bootstrap import load_environment  # noqa: E402

load_environment()

GRAPH = "https://graph.facebook.com/v21.0"

# Ice breakers (max 4). Keep them short — they show under the composer.
PROMPTS = [
    "🔎 Search the web",
    "📅 What's on my calendar today?",
    "⏰ Set a reminder",
    "📰 Daily briefing",
]

# Slash-command autocomplete (name ≤ 32 chars, description ≤ 256).
COMMANDS = [
    {"command_name": "menu", "command_description": "Show everything I can do"},
    {"command_name": "help", "command_description": "How to use this assistant"},
    {"command_name": "reset", "command_description": "Start a fresh conversation"},
]


def main() -> int:
    import urllib.error
    import urllib.request

    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_id:
        print("ERROR: WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID must be set.", file=sys.stderr)
        return 1

    payload = {
        "enable_welcome_message": True,
        "prompts": PROMPTS,
        "commands": COMMANDS,
    }
    req = urllib.request.Request(
        f"{GRAPH}/{phone_id}/conversational_automation",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print("OK:", resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"FAILED (HTTP {exc.code}): {exc.read().decode()}", file=sys.stderr)
        return 1
    print("\nConfigured welcome message + ice breakers + commands.")
    print("Open a fresh chat with the business number to see them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
