"""Regenerate the Google Calendar OAuth token (token.json).

Run this locally (it opens a browser for consent) whenever the calendar tools
stop loading with ``invalid_grant``. Google apps in "Testing" publishing mode
revoke refresh tokens after 7 days, so this needs to be re-run periodically —
or move the OAuth app to "In production" to stop the 7-day expiry.

    python scripts/refresh_google_token.py

The freshly written token.json is picked up by the worker container via the
mounted volume (see docker-compose.yml) without a rebuild — just restart it:

    docker compose restart worker
"""
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main() -> int:
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", str(ROOT / "credentials.json"))
    token_file = os.environ.get("GOOGLE_TOKEN_FILE", str(ROOT / "token.json"))

    if not os.path.exists(creds_file):
        print(f"ERROR: client secrets file not found: {creds_file}", file=sys.stderr)
        return 1

    print(f"Opening browser for Google consent (scopes: {SCOPES}) …")
    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=0)

    Path(token_file).write_text(creds.to_json())
    print(f"OK — wrote fresh token to {token_file}")
    print("Restart the worker:  docker compose restart worker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
