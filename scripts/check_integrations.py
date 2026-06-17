"""Live health-check for the two fragile integrations: Resend + Google Calendar.

Run it to confirm both features are actually ready to use (no message sent, no
event created). Exits non-zero if either is broken, so it doubles as a smoke
test in CI or a pre-flight check before debugging the agent.

    python scripts/check_integrations.py            # on the host (.env loaded)
    docker compose exec worker python scripts/check_integrations.py
"""
import os
import sys

# Make ``app`` importable and load .env exactly like the worker does.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.bootstrap import load_environment  # noqa: E402

load_environment()

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def check_resend() -> tuple[bool, str]:
    """Validate the Resend key without sending an email.

    GET /domains authenticates the key. A full-access key returns 200; a
    "Sending access" key is rejected with a *restricted_api_key* error — which
    still proves the key is valid and able to send.
    """
    import json
    import urllib.error
    import urllib.request

    key = os.environ.get("RESEND_API_KEY")
    if not key:
        return False, "RESEND_API_KEY not set"
    if not os.environ.get("RESEND_FROM_EMAIL"):
        return False, "RESEND_FROM_EMAIL not set"
    if not key.startswith("re_"):
        return False, "RESEND_API_KEY does not look like a Resend key (should start with 're_')"

    req = urllib.request.Request(
        "https://api.resend.com/domains",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True, "key valid (full access)"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = json.loads(exc.read()).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403) and "restrict" in body.lower():
            return True, "key valid (sending-only access)"
        return False, f"key rejected (HTTP {exc.code}: {body or 'invalid'}) — create a new API key"
    except Exception as exc:  # noqa: BLE001
        return False, f"network error reaching Resend: {exc}"


def check_whatsapp() -> tuple[bool, str]:
    """Validate the WhatsApp Cloud API token (it expires every ~24h in test mode)."""
    import json
    import urllib.error
    import urllib.request

    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not token:
        return False, "WHATSAPP_ACCESS_TOKEN not set"
    if not phone_id:
        return False, "WHATSAPP_PHONE_NUMBER_ID not set"

    req = urllib.request.Request(
        f"https://graph.facebook.com/v21.0/{phone_id}?fields=verified_name,display_phone_number",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        name = data.get("verified_name") or data.get("display_phone_number") or "ok"
        return True, f"token valid ({name})"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "token expired/invalid (code 190) — generate a new one in Meta → API Setup"
        return False, f"token check failed (HTTP {exc.code})"
    except Exception as exc:  # noqa: BLE001
        return False, f"network error reaching Graph API: {exc}"


def check_search() -> tuple[bool, str]:
    """Confirm the web-search backend is usable (Serper key, or DDG fallback)."""
    import json
    import urllib.error
    import urllib.request

    key = os.environ.get("SERPER_API_KEY")
    if not key:
        return True, "no SERPER_API_KEY — will use DuckDuckGo fallback (rate-limited)"

    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=json.dumps({"q": "ping", "num": 1}).encode(),
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            json.loads(resp.read())
        return True, "Serper key valid"
    except urllib.error.HTTPError as exc:
        return False, f"Serper key rejected (HTTP {exc.code}) — check SERPER_API_KEY"
    except Exception as exc:  # noqa: BLE001
        return False, f"network error reaching Serper: {exc}"


def check_calendar() -> tuple[bool, str]:
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
    if not os.path.exists(creds_file):
        return False, f"client secrets file missing: {creds_file}"
    if not os.path.exists(token_file):
        return False, f"token file missing: {token_file} — run scripts/refresh_google_token.py"

    try:
        from langchain_google_community.calendar.utils import get_google_credentials

        creds = get_google_credentials(
            token_file=token_file,
            client_secrets_file=creds_file,
            scopes=CALENDAR_SCOPES,
        )
    except Exception as exc:  # noqa: BLE001
        if "invalid_grant" in str(exc):
            return False, "token expired/revoked (invalid_grant) — run scripts/refresh_google_token.py"
        return False, f"calendar credential error: {exc}"
    return True, f"credentials valid (expired={getattr(creds, 'expired', '?')})"


def main() -> int:
    ok = True
    for name, fn in (
        ("WhatsApp API", check_whatsapp),
        ("Resend email", check_resend),
        ("Web search", check_search),
        ("Google Calendar", check_calendar),
    ):
        passed, detail = fn()
        ok = ok and passed
        print(f"[{'OK ' if passed else 'FAIL'}] {name}: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
