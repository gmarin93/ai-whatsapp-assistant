import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.config import get_settings
from app.services.handler import MessageHandler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["whatsapp"])

# One handler instance per process (build graph once)
_handler: MessageHandler | None = None


def get_handler() -> MessageHandler:
    global _handler
    if _handler is None:
        _handler = MessageHandler()
    return _handler


def _query_param(request: Request, *names: str) -> str:
    """Read a query param; Meta uses hub.mode or hub_mode depending on the request."""
    for name in names:
        value = request.query_params.get(name)
        if value:
            return value
    return ""


@router.get("/whatsapp")
async def verify_webhook(request: Request) -> Response:
    """Meta webhook verification (Developer Console → Verificar y guardar)."""
    hub_mode = _query_param(request, "hub.mode", "hub_mode")
    hub_verify_token = _query_param(request, "hub.verify_token", "hub_verify_token")
    hub_challenge = _query_param(request, "hub.challenge", "hub_challenge")

    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        logger.info("Webhook verified successfully")
        return Response(content=hub_challenge, media_type="text/plain")

    if not hub_mode and not hub_verify_token:
        logger.warning(
            "Webhook verify failed: no query params (did you open this URL in a browser? "
            "Use Meta → Verificar y guardar instead)"
        )
    else:
        logger.warning(
            "Webhook verify failed: hub.mode=%r token_match=%s",
            hub_mode,
            hub_verify_token == settings.whatsapp_verify_token,
        )
    raise HTTPException(status_code=403, detail="Verification failed")


def _validate_signature(body: bytes, signature: str | None) -> bool:
    """TODO: enforce in production. Returns True if check skipped (no secret)."""
    settings = get_settings()
    secret = settings.whatsapp_app_secret
    if not secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    received = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def _extract_text_messages(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Parse Meta webhook payload → list of (wa_id, text)."""
    results: list[tuple[str, str]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                if message.get("type") != "text":
                    continue
                wa_id = message.get("from", "")
                text = message.get("text", {}).get("body", "")
                if wa_id and text:
                    results.append((wa_id, text))
    return results


@router.post("/whatsapp")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Receive incoming messages; return 200 quickly, process in background."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not _validate_signature(body, signature):
        logger.warning("Webhook POST rejected: invalid X-Hub-Signature-256")
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body)
    messages = _extract_text_messages(payload)
    entry_id = payload.get("entry", [{}])[0].get("id", "?")
    phone_number_id = (
        payload.get("entry", [{}])[0]
        .get("changes", [{}])[0]
        .get("value", {})
        .get("metadata", {})
        .get("phone_number_id", "?")
    )
    logger.info(
        "Webhook POST: text_messages=%d entry_id=%s phone_number_id=%s",
        len(messages),
        entry_id,
        phone_number_id,
    )
    if entry_id == "0":
        logger.info(
            "This is Meta's SAMPLE test (entry_id=0). "
            "Reply in WhatsApp to +1 555 644-4691 to test your real number."
        )

    if not messages:
        field = (
            payload.get("entry", [{}])[0]
            .get("changes", [{}])[0]
            .get("field", "unknown")
        )
        logger.info("No text in payload (field=%s) — may be delivery/status update", field)

    handler = get_handler()

    for wa_id, text in messages:
        logger.info("Queuing message from wa_id=%s: %r", wa_id, text[:80])
        background_tasks.add_task(handler.handle_text, wa_id, text)

    return {"status": "ok"}
