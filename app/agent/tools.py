"""LangChain tools for the WhatsApp agent.

Search and email tools have been migrated to MCP servers (``app/mcp/``).
This module now only exposes supplemental tools that are difficult to serve
over stdio (e.g. Google Calendar, which requires OAuth state files on disk).

Use ``get_supplemental_tools()`` to retrieve them; the main entry point
(``app/main.py``) merges them with the MCP-backed tools at startup.
"""

import logging
import os

from langchain_core.tools import BaseTool

from app.config import get_settings

logger = logging.getLogger(__name__)

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_supplemental_tools() -> list[BaseTool]:
    """Return non-MCP, in-process tools (Google Calendar + reminders).

    These run in-process (not over stdio) because they need on-disk OAuth state
    (calendar) or the agent state + Redis pool (reminders). Calendar degrades to
    an empty list gracefully when credentials are missing.
    """
    from app.agent.reminders import get_reminder_tools

    return _get_calendar_tools() + get_reminder_tools()


# ── Google Calendar ──────────────────────────────────────────────────────────

def _get_calendar_tools() -> list[BaseTool]:
    """Load Google Calendar tools using configurable credential paths.

    Returns an empty list (never raises) when:
      - the client secrets / token files are missing, or
      - the cached token's refresh token is expired or revoked.

    OAuth tokens for Google apps in "Testing" mode expire after 7 days; when
    that happens, regenerate ``token.json`` locally (the consent flow opens a
    browser) and the mounted volume will pick it up without a rebuild.
    """
    settings = get_settings()
    creds_file = settings.google_credentials_file
    token_file = settings.google_token_file

    if not os.path.exists(creds_file):
        logger.info(
            "Google Calendar disabled — client secrets file %r not found", creds_file
        )
        return []
    if not os.path.exists(token_file):
        logger.info(
            "Google Calendar disabled — token file %r not found. Run the OAuth "
            "flow locally to generate it.",
            token_file,
        )
        return []

    try:
        from langchain_google_community.calendar.utils import (
            build_calendar_service,
            get_google_credentials,
        )

        credentials = get_google_credentials(
            token_file=token_file,
            client_secrets_file=creds_file,
            scopes=CALENDAR_SCOPES,
        )
        service = build_calendar_service(credentials=credentials)
        tools = _build_calendar_tools(service)
        logger.info("Google Calendar tools loaded: %s", [t.name for t in tools])
        return tools
    except Exception as exc:
        if "invalid_grant" in str(exc):
            logger.warning(
                "Google Calendar token %r is expired or revoked (invalid_grant). "
                "Regenerate it locally and restart — the mounted volume will pick "
                "up the new token.",
                token_file,
            )
        else:
            logger.warning("Google Calendar tools not available: %s", exc)
        return []


# We build our own thin tools over the Google Calendar API instead of using
# langchain_google_community's CalendarToolkit: its SearchEvents tool asks the
# LLM to pass a JSON *string* of calendar info and then ``json.loads`` it, which
# blows up on any stray control character (e.g. a newline in a calendar's
# description). These two tools are simpler, more reliable, and easier to demo.

CALENDAR_TZ = "America/Costa_Rica"
DEFAULT_CALENDAR_ID = "primary"


def _to_iso(value: str) -> str:
    """Normalise an ISO-8601 string to a TZ-aware ISO timestamp (assume CR tz)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(CALENDAR_TZ))
    return dt.isoformat()


def _build_calendar_tools(service) -> list[BaseTool]:
    from langchain_core.tools import StructuredTool

    def list_calendar_events(start: str, end: str) -> str:
        """List Google Calendar events between two times.

        Args:
            start: Start of the window, ISO-8601 in America/Costa_Rica
                (e.g. "2026-06-17T00:00:00").
            end: End of the window, ISO-8601 in America/Costa_Rica
                (e.g. "2026-06-17T23:59:59").
        """
        try:
            result = (
                service.events()
                .list(
                    calendarId=DEFAULT_CALENDAR_ID,
                    timeMin=_to_iso(start),
                    timeMax=_to_iso(end),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=20,
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            return f"error: could not fetch events ({exc})"

        items = result.get("items", [])
        if not items:
            return "No events found in that range."
        lines = []
        for e in items:
            when = e["start"].get("dateTime", e["start"].get("date", ""))
            lines.append(f"- {when}: {e.get('summary', '(no title)')}")
        return "\n".join(lines)

    def create_calendar_event(
        summary: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
    ) -> str:
        """Create a Google Calendar event.

        Args:
            summary: Event title.
            start: Start time, ISO-8601 in America/Costa_Rica.
            end: End time, ISO-8601 in America/Costa_Rica.
            description: Optional event description.
            location: Optional event location.
        """
        body = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": _to_iso(start), "timeZone": CALENDAR_TZ},
            "end": {"dateTime": _to_iso(end), "timeZone": CALENDAR_TZ},
        }
        try:
            created = service.events().insert(
                calendarId=DEFAULT_CALENDAR_ID, body=body
            ).execute()
        except Exception as exc:  # noqa: BLE001
            return f"error: could not create event ({exc})"
        return f"Event created: {created.get('summary', summary)} — {created.get('htmlLink', '')}".strip(" —")

    return [
        StructuredTool.from_function(list_calendar_events),
        StructuredTool.from_function(create_calendar_event),
    ]
