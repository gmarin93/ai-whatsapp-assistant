"""ARQ worker — processes WhatsApp messages from the Redis queue.

Start with:
    arq app.worker.WorkerSettings

Each worker process:
  1. Opens persistent MCP sessions (one subprocess per MCP server).
  2. Builds the LangGraph agent with a Redis checkpointer.
  3. Picks up ``process_message`` jobs from the queue.
  4. Acquires a per-user Redis lock so two workers never race on the same user.
  5. Sends the AI reply via the WhatsApp Cloud API.
"""
import logging

from app.bootstrap import load_environment

load_environment()

from arq import ArqRedis, cron
from arq.connections import RedisSettings

from app.config import get_settings

logger = logging.getLogger(__name__)


# ── Job ───────────────────────────────────────────────────────────────────────

async def process_message(ctx: dict, wa_id: str, text: str) -> None:
    """Dequeued job: run the agent and reply to the user."""
    settings = get_settings()
    redis: ArqRedis = ctx["redis"]
    handler = ctx["handler"]

    lock_key = f"whatsapp:lock:{wa_id}"
    lock_ttl = settings.user_lock_ttl_seconds * 1000  # milliseconds for Redis SET PX

    # Per-user lock — skip if another worker is already processing this user.
    acquired = await redis.set(lock_key, "1", px=lock_ttl, nx=True)
    if not acquired:
        logger.warning(
            "wa_id=%s already being processed by another worker; re-queuing", wa_id
        )
        # Re-enqueue with a short delay so the message isn't lost. The queue name
        # MUST match WorkerSettings.queue_name, otherwise the job lands on the
        # default queue and is never picked up by this worker.
        await redis.enqueue_job(
            "process_message",
            wa_id,
            text,
            _defer_by=2,
            _queue_name=settings.arq_queue_name,
        )
        return

    try:
        await handler.handle_text(wa_id, text)
    finally:
        await redis.delete(lock_key)


async def process_audio(ctx: dict, wa_id: str, media_id: str) -> None:
    """Dequeued job: transcribe a voice note and reply to the user.

    Mirrors ``process_message``'s per-user lock so a voice note and a text from
    the same user can't be processed concurrently by two workers.
    """
    settings = get_settings()
    redis: ArqRedis = ctx["redis"]
    handler = ctx["handler"]

    lock_key = f"whatsapp:lock:{wa_id}"
    lock_ttl = settings.user_lock_ttl_seconds * 1000

    acquired = await redis.set(lock_key, "1", px=lock_ttl, nx=True)
    if not acquired:
        logger.warning("wa_id=%s busy; re-queuing voice note", wa_id)
        await redis.enqueue_job(
            "process_audio", wa_id, media_id, _defer_by=2, _queue_name=settings.arq_queue_name
        )
        return

    try:
        await handler.handle_audio(wa_id, media_id)
    finally:
        await redis.delete(lock_key)


async def send_reminder(ctx: dict, wa_id: str, message: str) -> None:
    """Deferred job: deliver a scheduled reminder back to the user.

    Enqueued by the ``schedule_reminder`` tool with ``_defer_by`` set to the
    delay until the reminder time (see ``app/agent/reminders.py``).
    """
    from app.meta.client import WhatsAppClient

    logger.info("Delivering reminder to wa_id=%s", wa_id)
    await WhatsAppClient().send_text(wa_id, f"⏰ Reminder: {message}")


async def daily_briefing(ctx: dict) -> None:
    """Cron job: send each configured user a morning briefing.

    Also registered in ``functions`` so it can be triggered on demand for
    testing (``scripts/trigger_briefing.py``). With multiple worker replicas,
    arq's ``cron(..., unique=True)`` (the default) ensures only one worker runs
    it per scheduled tick.
    """
    from app.agent.briefing import run_briefing

    settings = get_settings()
    handler = ctx.get("handler")
    if handler is None:
        logger.warning("daily_briefing: no handler in worker context — skipping")
        return

    wa_ids = [x.strip() for x in settings.briefing_wa_ids.split(",") if x.strip()]
    if not wa_ids:
        logger.info("daily_briefing: BRIEFING_WA_IDS not set — skipping")
        return

    await run_briefing(handler, wa_ids)


# ── Startup / shutdown hooks ──────────────────────────────────────────────────

async def startup(ctx: dict) -> None:
    """Called once per worker process at launch."""
    from app.mcp.loader import init_mcp_pool
    from app.agent.tools import get_supplemental_tools
    from app.services.handler import MessageHandler

    logger.info("[worker] Loading MCP tools …")
    mcp_tools = await init_mcp_pool()
    supplemental = get_supplemental_tools()
    all_tools = mcp_tools + supplemental

    logger.info("[worker] Building agent with %d tool(s)", len(all_tools))
    handler = MessageHandler(tools=all_tools)

    ctx["handler"] = handler
    logger.info("[worker] Ready")


async def shutdown(ctx: dict) -> None:
    """Called once per worker process on graceful shutdown."""
    from app.mcp.loader import close_mcp_pool
    await close_mcp_pool()
    logger.info("[worker] MCP pool closed")


# ── ARQ WorkerSettings ────────────────────────────────────────────────────────

def _redis_settings() -> RedisSettings:
    settings = get_settings()
    url = settings.redis_url or "redis://localhost:6379"
    return RedisSettings.from_dsn(url)


class WorkerSettings:
    functions = [process_message, process_audio, send_reminder, daily_briefing]
    cron_jobs = [
        # 07:00 America/Costa_Rica (UTC-6). unique=True → one replica runs it.
        cron(daily_briefing, hour=get_settings().briefing_cron_hour_utc, minute=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    queue_name = get_settings().arq_queue_name
    max_jobs = 10          # concurrent jobs per worker process
    job_timeout = 120      # seconds before a job is considered hung
    keep_result = 300      # seconds to keep job results in Redis
