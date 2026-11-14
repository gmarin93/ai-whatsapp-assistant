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
import asyncio
from uuid import uuid4
from app.bootstrap import load_environment
from app.services.redis_user_lock import RedisUserLock

load_environment()

from arq import ArqRedis, cron
from arq.connections import RedisSettings
from arq import Retry
from app.config import get_settings

logger = logging.getLogger(__name__)


# ── Job ───────────────────────────────────────────────────────────────────────


async def process_message(
    ctx: dict,
    wa_id: str,
    text: str,
    message_id: str | None = None,
    phone_number_id: str | None = None,
) -> None:
    """Process WhatsApp messages with Redis locking and optional n8n routing."""
    settings = get_settings()
    if phone_number_id:
        logger.info(
            "Processing WhatsApp message: phone_number_id=%s, message_id=%s",
            phone_number_id,
            message_id,
    )
    redis: ArqRedis = ctx["redis"]
    handler = ctx["handler"]

    lock = RedisUserLock(
        redis=redis,
        wa_id=wa_id,
        ttl_seconds=settings.user_lock_ttl_seconds,
    )

    acquired = await lock.acquire()

    if not acquired:
        logger.warning(
            "wa_id=%s already being processed; re-queuing",
            wa_id,
        )

        try:
            job = await redis.enqueue_job(
                "process_message",
                wa_id,
                text,
                message_id,
                _defer_by=2,
                _queue_name=settings.arq_queue_name,
            )

            if job is None:
                logger.error(
                    "ARQ did not create the deferred job: "
                    "wa_id=%s, message_id=%s",
                    wa_id,
                    message_id,
                )
                raise Retry(defer=2)

        except Retry:
            raise

        except Exception as exc:
            logger.exception(
                "Failed to re-queue busy user message: "
                "wa_id=%s, message_id=%s",
                wa_id,
                message_id,
            )
            raise Retry(defer=2) from exc

        return

    try:
        lock.start_renewal()

        # Route selected users through n8n when the dispatcher is available.
        dispatcher = ctx.get("n8n_dispatcher")

        if dispatcher is not None and message_id:
            try:
               handled = await dispatcher.dispatch(
                    message_id=message_id,
                    wa_id=wa_id,
                    text=text,
                    phone_number_id=phone_number_id
                )

            except RuntimeError as exc:
                logger.exception(
                    "n8n dispatch failed: message_id=%s",
                    message_id,
                )

                # Retry only when the processing claim was removed.
                deduplication = getattr(
                    dispatcher,
                    "deduplication",
                    None,
                )

                if deduplication is None:
                    logger.error(
                        "Cannot retry n8n message without deduplication: %s",
                        message_id,
                    )
                    raise

                try:
                    status = await deduplication.get_status(
                        message_id,
                    )

                except Exception:
                    logger.exception(
                        "Cannot verify n8n message state: %s",
                        message_id,
                    )
                    raise RuntimeError(
                        "Cannot safely retry n8n message"
                    ) from exc

                if status is not None:
                    logger.error(
                        "n8n retry blocked because Redis still "
                        "contains message_id=%s, status=%s",
                        message_id,
                        status,
                    )
                    raise

                # ARQ handles the deferred retry and its attempt limit.
                retry_attempt = ctx.get("job_try", 1)
                retry_delay = min(retry_attempt * 5, 30)

                logger.warning(
                    "Scheduling n8n retry: "
                    "message_id=%s, attempt=%s, delay=%ss",
                    message_id,
                    retry_attempt,
                    retry_delay,
                )

                raise Retry(defer=retry_delay) from exc

            if handled:
                logger.info(
                    "Message handled by n8n: message_id=%s",
                    message_id,
                )
                return

        # Preserve the original agent behavior for all other messages.
        await handler.handle_text(wa_id, text)

    except asyncio.CancelledError:
        if lock.lost:
            logger.error(
                "Message processing cancelled after losing Redis lock: "
                "wa_id=%s, message_id=%s",
                wa_id,
                message_id,
            )
        raise

    finally:
        await lock.stop_renewal()

        try:
            released = await lock.release()

            if not released:
                logger.warning(
                    "User lock ownership changed: wa_id=%s",
                    wa_id,
                )

        except Exception:
            logger.exception(
                "Failed to release Redis user lock: wa_id=%s",
                wa_id,
            )


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
    """Initialize the original agent and optional n8n integration."""
    from app.mcp.loader import init_mcp_pool
    from app.agent.tools import get_supplemental_tools
    from app.services.handler import MessageHandler

    settings = get_settings()

    logger.info("[worker] Loading MCP tools...")
    mcp_tools = await init_mcp_pool()
    supplemental = get_supplemental_tools()
    all_tools = mcp_tools + supplemental

    logger.info("[worker] Building agent with %d tool(s)", len(all_tools))

    handler = MessageHandler(tools=all_tools)
    ctx["handler"] = handler

    # Keep the original workflow unchanged when n8n is disabled.
    ctx["n8n_dispatcher"] = None

    if settings.n8n_enabled:
        from app.meta.client import WhatsAppClient
        from app.services.n8n_client import N8NClient
        from app.services.n8n_deduplication import N8NDeduplication
        from app.services.n8n_dispatcher import N8NDispatcher

        logger.info("[worker] Initializing n8n integration")

        redis = ctx["redis"]

        n8n_client = N8NClient(
            webhook_url=settings.n8n_webhook_url,
            api_key=settings.n8n_api_key,
            timeout=settings.n8n_timeout_seconds,
        )

        deduplication = N8NDeduplication(
            redis=redis,
            ttl_seconds=300,
        )

        whatsapp_client = WhatsAppClient()

        ctx["n8n_dispatcher"] = N8NDispatcher(
            settings=settings,
            n8n_client=n8n_client,
            send_reply=whatsapp_client.send_text,
            deduplication=deduplication,
        )

        logger.info("[worker] n8n integration initialized")

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
