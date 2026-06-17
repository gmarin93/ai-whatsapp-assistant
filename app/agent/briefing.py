"""Daily morning briefing.

Reuses the already-built agent (with its calendar + search tools) to assemble a
short proactive message: today's calendar events, today's World Cup matches, and
today's weather. Scheduled by an ARQ cron job in ``app/worker.py``; the cron
hands us the live ``MessageHandler`` from its context.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ = ZoneInfo("America/Costa_Rica")


def build_briefing_prompt(now: datetime | None = None) -> str:
    today = (now or datetime.now(TZ)).strftime("%A, %B %d, %Y")
    return (
        f"Generate my morning briefing for {today}. Use your tools to gather the "
        "info, then write ONE short, friendly WhatsApp message with these sections:\n"
        "1. 📅 Today's calendar events — use the calendar tool; say 'nothing scheduled' if empty.\n"
        "2. ⚽ Today's FIFA World Cup 2026 matches — search the web for today's fixtures and kickoff times.\n"
        "3. ☀️ Weather in San José, Costa Rica today — search the web.\n"
        "Keep it concise and scannable with the emojis as section headers. "
        "Do NOT send an email. Start with a brief 'Good morning!'."
    )


async def run_briefing(handler, wa_ids: list[str]) -> None:
    """Build the briefing once per user and deliver it over WhatsApp."""
    prompt = build_briefing_prompt()
    for wa_id in wa_ids:
        try:
            logger.info("Building daily briefing for wa_id=%s", wa_id)
            reply = await handler.agent.reply(wa_id, prompt, thread_id=f"briefing:{wa_id}")
            await handler.whatsapp.send_text(wa_id, reply)
            logger.info("Sent daily briefing to wa_id=%s", wa_id)
        except Exception:
            logger.exception("Daily briefing failed for wa_id=%s", wa_id)
