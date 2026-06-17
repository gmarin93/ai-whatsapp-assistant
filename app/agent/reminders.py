"""In-process LangChain tool: schedule a WhatsApp reminder.

Unlike the MCP-backed tools (search, email), this lives in-process because it
needs two things an stdio subprocess can't easily get:

  * the current user's ``wa_id`` — injected from the agent state, not the LLM, and
  * the ARQ/Redis pool used to enqueue the delayed job.

The agent calls ``schedule_reminder`` with a future time; this enqueues a
deferred ``send_reminder`` job. That job runs in the ARQ worker at the scheduled
time and messages the user back (see ``app/worker.py``).
"""
import logging
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

from app.config import get_settings

logger = logging.getLogger(__name__)

TZ = ZoneInfo("America/Costa_Rica")
MAX_DELAY_SECONDS = 60 * 60 * 24 * 30  # cap reminders at 30 days out


@tool
async def schedule_reminder(
    when: str,
    message: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """Schedule a WhatsApp reminder to be sent back to the user at a later time.

    Use this whenever the user asks to be reminded of something ("remind me to
    …", "ping me in 2 hours", "tomorrow at 9am tell me to …").

    Args:
        when: The reminder time as an ISO-8601 datetime in the America/Costa_Rica
            timezone, e.g. "2026-06-17T09:00:00". Compute it from the current
            date/time given in your system prompt.
        message: What to remind the user about, phrased as the reminder text.
    """
    wa_id = (state or {}).get("wa_id")
    if not wa_id:
        return "error: could not determine who to remind."

    try:
        when_dt = datetime.fromisoformat(when)
    except ValueError:
        return f"error: '{when}' is not a valid ISO-8601 datetime (e.g. 2026-06-17T09:00:00)."
    if when_dt.tzinfo is None:
        when_dt = when_dt.replace(tzinfo=TZ)

    delay = (when_dt - datetime.now(TZ)).total_seconds()
    if delay <= 0:
        return "error: that time is in the past — pick a future time."
    if delay > MAX_DELAY_SECONDS:
        return "error: reminders can be at most 30 days out."

    settings = get_settings()
    if not settings.redis_url:
        return "error: reminders are unavailable (Redis is not configured)."

    from arq.connections import RedisSettings, create_pool

    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job(
            "send_reminder",
            wa_id,
            message,
            _defer_by=delay,
            _queue_name=settings.arq_queue_name,
        )
    finally:
        await pool.close()

    human = when_dt.strftime("%A, %B %d at %I:%M %p")
    logger.info("Scheduled reminder for wa_id=%s in %.0fs: %s", wa_id, delay, message)
    return f"Reminder set for {human} (Costa Rica time)."


def get_reminder_tools() -> list[BaseTool]:
    """Return the in-process reminder tool(s)."""
    return [schedule_reminder]
