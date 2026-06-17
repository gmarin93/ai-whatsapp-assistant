import hashlib
import hmac
import json
import logging
from typing import Any

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


@router.get("/whatsapp")
async def verify_webhook(request: Request) -> Response:
    """Meta webhook verification."""
    hub_mode = _query_param(request, "hub.mode", "hub_mode")
    hub_verify_token = _query_param(request, "hub.verify_token", "hub_verify_token")
    hub_challenge = _query_param(request, "hub.challenge", "hub_challenge")

    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        logger.info("Webhook verified successfully")
        return Response(content=hub_challenge, media_type="text/plain")

    logger.warning(
        "Webhook verify failed: hub.mode=%r token_match=%s",
        hub_mode,
        hub_verify_token == settings.whatsapp_verify_token,
    )
    raise HTTPException(status_code=403, detail="Verification failed")


def _validate_signature(body: bytes, signature: str | None) -> bool:
    settings = get_settings()
    secret = settings.whatsapp_app_secret
    if not secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    received = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def _message_text(message: dict[str, Any]) -> str:
    """Extract user intent as text from a text OR interactive (tapped) message.

    A tapped reply button / list item is mapped to a natural-language
    instruction so taps flow through the same agent pipeline as typed text.
    """
    mtype = message.get("type")
    if mtype == "text":
        return message.get("text", {}).get("body", "")
    if mtype == "request_welcome":
        # Fired when a user opens the chat for the first time (if the welcome
        # message is enabled). Route it to the menu.
        return "/menu"
    if mtype == "interactive":
        from app.agent.menu import expand_tap

        inter = message.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        if reply:
            return expand_tap(reply.get("id", ""), reply.get("title", ""))
    return ""


def _parse_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Parse one Meta message into {id, wa_id, text?} or {id, wa_id, audio_id?}."""
    wa_id = message.get("from", "")
    if not wa_id:
        return None
    msg_id = message.get("id", "")

    if message.get("type") == "audio":
        media_id = message.get("audio", {}).get("id")
        return {"id": msg_id, "wa_id": wa_id, "audio_id": media_id} if media_id else None

    text = _message_text(message)
    return {"id": msg_id, "wa_id": wa_id, "text": text} if text else None


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Meta webhook payload → list of {id, wa_id, text|audio_id} dicts."""
    results: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                parsed = _parse_message(message)
                if parsed:
                    results.append(parsed)
    return results


async def _is_duplicate(msg_id: str) -> bool:
    """Return True if this message ID was already seen (stored in Redis)."""
    if not _arq_pool or not msg_id:
        return False
    settings = get_settings()
    key = f"whatsapp:seen:{msg_id}"
    # SET NX EX — only sets if key doesn't exist; returns True on new, None on duplicate
    new = await _arq_pool.set(key, "1", ex=settings.dedup_ttl_seconds, nx=True)
    return not new


@router.post("/whatsapp")
async def receive_webhook(request: Request) -> dict[str, str]:
    """Receive incoming messages; return 200 quickly, enqueue processing."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not _validate_signature(body, signature):
        logger.warning("Webhook POST rejected: invalid X-Hub-Signature-256")
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body)
    messages = _extract_messages(payload)

    entry_id = payload.get("entry", [{}])[0].get("id", "?")
    logger.info("Webhook POST: %d text message(s), entry_id=%s", len(messages), entry_id)

    if not messages:
        field = (
            payload.get("entry", [{}])[0]
            .get("changes", [{}])[0]
            .get("field", "unknown")
        )
        logger.info("No text in payload (field=%s) — may be delivery/status update", field)

    for msg in messages:
        msg_id, wa_id = msg["id"], msg["wa_id"]
        if await _is_duplicate(msg_id):
            logger.info("Duplicate message %s from wa_id=%s — skipping", msg_id, wa_id)
            continue

        audio_id = msg.get("audio_id")
        kind = "voice note" if audio_id else "message"
        logger.info("Enqueueing %s %s from wa_id=%s", kind, msg_id, wa_id)

        if _arq_pool is not None:
            settings = get_settings()
            if audio_id:
                await _arq_pool.enqueue_job(
                    "process_audio", wa_id, audio_id, _queue_name=settings.arq_queue_name
                )
            else:
                await _arq_pool.enqueue_job(
                    "process_message", wa_id, msg["text"], _queue_name=settings.arq_queue_name
                )
        elif _fallback_handler is not None:
            # Dev mode: no Redis — run directly as a background task
            import asyncio

            if audio_id:
                coro = _fallback_handler.handle_audio(wa_id, audio_id)
            else:
                coro = _fallback_handler.handle_text(wa_id, msg["text"])
            task = asyncio.create_task(coro)
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        else:
            logger.error("No handler available — message from wa_id=%s dropped", wa_id)

    return {"status": "ok"}
