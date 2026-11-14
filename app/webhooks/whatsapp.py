
import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import uuid4

from arq.jobs import Job, JobStatus
from fastapi import APIRouter, HTTPException, Request, Response

from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["whatsapp"])

# Injected at startup — either an ArqRedis pool (production) or a
# fallback MessageHandler (dev / no-Redis mode).
_arq_pool = None
_fallback_handler = None

# Strong references to in-flight dev-mode background tasks so the event loop
# does not garbage-collect them mid-execution (asyncio keeps only weak refs).
_background_tasks: set = set()


def set_arq_pool(pool) -> None:
    global _arq_pool
    _arq_pool = pool


def set_fallback_handler(handler) -> None:
    """Used when Redis is not available (dev mode)."""
    global _fallback_handler
    _fallback_handler = handler


def _query_param(request: Request, *names: str) -> str:
    for name in names:
        value = request.query_params.get(name)
        if value:
            return value
    return ""


async def _get_arq_job_status(
    msg_id: str,
    queue_name: str,
) -> JobStatus | None:
    """Check an ARQ job after an uncertain enqueue outcome."""
    if _arq_pool is None or not msg_id:
        return None

    try:
        job = Job(
            f"whatsapp:{msg_id}",
            _arq_pool,
            _queue_name=queue_name,
        )
        return await job.status()
    except Exception:
        logger.exception(
            "Unable to verify ARQ job status: %s",
            msg_id,
        )
        return None


@router.get("/whatsapp")
async def verify_webhook(request: Request) -> Response:
    """Meta webhook verification."""
    hub_mode = _query_param(request, "hub.mode", "hub_mode")
    hub_verify_token = _query_param(
        request,
        "hub.verify_token",
        "hub_verify_token",
    )
    hub_challenge = _query_param(
        request,
        "hub.challenge",
        "hub_challenge",
    )

    settings = get_settings()

    if (
        hub_mode == "subscribe"
        and hub_verify_token == settings.whatsapp_verify_token
    ):
        logger.info("Webhook verified successfully")
        return Response(
            content=hub_challenge,
            media_type="text/plain",
        )

    logger.warning(
        "Webhook verify failed: hub.mode=%r token_match=%s",
        hub_mode,
        hub_verify_token == settings.whatsapp_verify_token,
    )

    raise HTTPException(
        status_code=403,
        detail="Verification failed",
    )


def _validate_signature(body: bytes, signature: str | None) -> bool:
    settings = get_settings()
    secret = settings.whatsapp_app_secret

    if not secret:
        return True

    if not signature or not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    received = signature.removeprefix("sha256=")

    return hmac.compare_digest(expected, received)


def _message_text(message: dict[str, Any]) -> str:
    """Extract user intent from text or interactive messages.

    A tapped reply button or list item is mapped to a natural-language
    instruction so taps flow through the same agent pipeline as typed text.
    """
    mtype = message.get("type")

    if mtype == "text":
        return message.get("text", {}).get("body", "")

    if mtype == "request_welcome":
        # Route first-time welcome messages to the menu.
        return "/menu"

    if mtype == "interactive":
        from app.agent.menu import expand_tap

        inter = message.get("interactive", {})
        reply = (
            inter.get("button_reply")
            or inter.get("list_reply")
            or {}
        )

        if reply:
            return expand_tap(
                reply.get("id", ""),
                reply.get("title", ""),
            )

    return ""


def _parse_message(
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """Parse one Meta message into text or audio job data."""
    wa_id = message.get("from", "")

    if not wa_id:
        return None

    msg_id = message.get("id", "")

    if message.get("type") == "audio":
        media_id = message.get("audio", {}).get("id")

        return (
            {
                "id": msg_id,
                "wa_id": wa_id,
                "audio_id": media_id,
            }
            if media_id
            else None
        )

    text = _message_text(message)

    return (
        {
            "id": msg_id,
            "wa_id": wa_id,
            "text": text,
        }
        if text
        else None
    )



def _extract_messages(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract supported WhatsApp messages with channel metadata."""
    results: list[dict[str, Any]] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id", "")

            for message in value.get("messages", []):
                parsed = _parse_message(message)

                if parsed:
                    parsed["phone_number_id"] = phone_number_id
                    results.append(parsed)

    return results



async def _claim_message(msg_id: str) -> str | None:
    """Atomically reserve a message before enqueueing it."""
    if not _arq_pool or not msg_id:
        return None

    settings = get_settings()
    token = uuid4().hex

    acquired = await _arq_pool.set(
        f"whatsapp:seen:{msg_id}",
        token,
        ex=settings.dedup_ttl_seconds,
        nx=True,
    )

    return token if acquired else None


async def _release_message_claim(
    msg_id: str,
    token: str,
) -> bool:
    """Release a message reservation only when its token matches."""
    if not _arq_pool or not msg_id or not token:
        return False

    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    result = await _arq_pool.eval(
        script,
        1,
        f"whatsapp:seen:{msg_id}",
        token,
    )

    return bool(result)


@router.post("/whatsapp")
async def receive_webhook(
    request: Request,
) -> dict[str, str]:
    """Receive incoming messages and enqueue them safely."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not _validate_signature(body, signature):
        logger.warning(
            "Webhook POST rejected: invalid X-Hub-Signature-256"
        )

        raise HTTPException(
            status_code=403,
            detail="Invalid signature",
        )

    payload = json.loads(body)
    messages = _extract_messages(payload)

    entry_id = payload.get("entry", [{}])[0].get("id", "?")

    logger.info(
        "Webhook POST: %d message(s), entry_id=%s",
        len(messages),
        entry_id,
    )

    for msg in messages:
        msg_id = msg["id"]
        wa_id = msg["wa_id"]
        audio_id = msg.get("audio_id")

        if _arq_pool is not None:
            settings = get_settings()

            if not msg_id:
                logger.error(
                    "Message rejected: missing Meta message ID"
                )

                raise HTTPException(
                    status_code=503,
                    detail="Message ID required",
                )

            # Reserve the message before adding it to the queue.
            token = await _claim_message(msg_id)

           
            if token is None:
                # A Redis claim does not guarantee the job exists.
                job_status = await _get_arq_job_status(
                    msg_id,
                    settings.arq_queue_name,
                )

                if job_status in (
                    JobStatus.queued,
                    JobStatus.in_progress,
                    JobStatus.deferred,
                    JobStatus.complete,
                ):
                    logger.info(
                        "Duplicate WhatsApp message confirmed in ARQ: "
                        "message_id=%s, status=%s",
                        msg_id,
                        job_status.value,
                    )
                    continue

                logger.error(
                    "WhatsApp claim exists but ARQ job is unconfirmed: "
                    "message_id=%s, status=%s",
                    msg_id,
                    job_status,
                )

                raise HTTPException(
                    status_code=503,
                    detail="Message processing status uncertain",
                )


            try:
                if audio_id:
                    job = await _arq_pool.enqueue_job(
                        "process_audio",
                        wa_id,
                        audio_id,
                        _job_id=f"whatsapp:{msg_id}",
                        _queue_name=settings.arq_queue_name,
                    )
               
                else:
                    job = await _arq_pool.enqueue_job(
                        "process_message",
                        wa_id,
                        msg["text"],
                        msg_id,
                        msg.get("phone_number_id"),
                        _job_id=f"whatsapp:{msg_id}",
                        _queue_name=settings.arq_queue_name,
                    )


                if job is None:
                    # ARQ already has a job with this message ID.
                    logger.info(
                        "WhatsApp message already enqueued: %s",
                        msg_id,
                    )
                    continue


            except Exception as exc:
                logger.exception(
                    "Failed to enqueue WhatsApp message: %s",
                    msg_id,
                )

                # Check whether ARQ accepted the job before the error.
                job_status = await _get_arq_job_status(
                    msg_id,
                    settings.arq_queue_name,
                )

                if job_status in (
                    JobStatus.queued,
                    JobStatus.in_progress,
                    JobStatus.deferred,
                    JobStatus.complete,
                ):
                    logger.warning(
                        "ARQ job found after enqueue failure: "
                        "message_id=%s, status=%s",
                        msg_id,
                        job_status.value,
                    )
                    continue

                # An unknown or missing status does not prove that
                # the job was never accepted. Preserve the claim.
                logger.error(
                    "ARQ enqueue outcome uncertain; preserving "
                    "message claim: message_id=%s, status=%s",
                    msg_id,
                    job_status,
                )

                raise HTTPException(
                    status_code=503,
                    detail="Message queue unavailable",
                ) from exc


        elif _fallback_handler is not None:
            # Development mode without ARQ.
            import asyncio

            if audio_id:
                coro = _fallback_handler.handle_audio(
                    wa_id,
                    audio_id,
                )
            else:
                coro = _fallback_handler.handle_text(
                    wa_id,
                    msg["text"],
                )

            task = asyncio.create_task(coro)
            _background_tasks.add(task)
            task.add_done_callback(
                _background_tasks.discard
            )

        else:
            logger.error(
                "No handler available for wa_id=%s",
                wa_id,
            )

            raise HTTPException(
                status_code=503,
                detail="Message handler unavailable",
            )

    return {"status": "ok"}
