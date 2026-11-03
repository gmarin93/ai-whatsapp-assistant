
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.services.n8n_deduplication import N8NDeduplication
from app.services.n8n_dispatcher import N8NDispatcher
from app.worker import process_message


@pytest.mark.asyncio
async def test_authorized_message_end_to_end():
    """Process one authorized message without invoking the original agent."""
    settings = Settings(
        _env_file=None,
        n8n_enabled=True,
        n8n_webhook_url="http://localhost:5678/webhook/test",
        n8n_api_key="test-secret",
        n8n_allowed_wa_ids="50688888888",
    )

    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    handler = AsyncMock()

    n8n_client = AsyncMock()
    n8n_client.generate_reply.return_value = "Hello from n8n"

    send_reply = AsyncMock()
    send_reply.return_value = {
        "messaging_product": "whatsapp",
        "messages": [{"id": "wamid.meta-response-001"}],
    }

    deduplication = N8NDeduplication(
        redis=redis,
        ttl_seconds=300,
    )

    dispatcher = N8NDispatcher(
        settings=settings,
        n8n_client=n8n_client,
        send_reply=send_reply,
        deduplication=deduplication,
    )

    ctx = {
        "redis": redis,
        "handler": handler,
        "n8n_dispatcher": dispatcher,
    }

    with patch("app.worker.get_settings", return_value=settings):
        await process_message(
            ctx,
            "50688888888",
            "Hello",
            "wamid.incoming-001",
        )

    n8n_client.generate_reply.assert_awaited_once_with(
        message_id="wamid.incoming-001",
        wa_id="50688888888",
        text="Hello",
    )

    send_reply.assert_awaited_once_with(
        "50688888888",
        "Hello from n8n",
    )

    handler.handle_text.assert_not_awaited()

    # Verify that Redis was used for claim and state transitions.
    assert redis.set.await_count >= 2
    assert redis.eval.await_count >= 3


@pytest.mark.asyncio
async def test_duplicate_message_does_not_send_another_reply():
    """Skip duplicate messages without invoking n8n or the original agent."""
    settings = Settings(
        _env_file=None,
        n8n_enabled=True,
        n8n_webhook_url="http://localhost:5678/webhook/test",
        n8n_api_key="test-secret",
        n8n_allowed_wa_ids="50688888888",
    )

    redis = AsyncMock()
    redis.set.side_effect = [
        True,   # User lock acquired
        None,   # Message already claimed by another process
    ]
    redis.eval.return_value = 1

    handler = AsyncMock()
    n8n_client = AsyncMock()
    send_reply = AsyncMock()

    deduplication = N8NDeduplication(
        redis=redis,
        ttl_seconds=300,
    )

    dispatcher = N8NDispatcher(
        settings=settings,
        n8n_client=n8n_client,
        send_reply=send_reply,
        deduplication=deduplication,
    )

    ctx = {
        "redis": redis,
        "handler": handler,
        "n8n_dispatcher": dispatcher,
    }

    with patch("app.worker.get_settings", return_value=settings):
        await process_message(
            ctx,
            "50688888888",
            "Hello",
            "wamid.duplicate-001",
        )

    n8n_client.generate_reply.assert_not_awaited()
    send_reply.assert_not_awaited()
    handler.handle_text.assert_not_awaited()

    assert redis.set.await_count == 2
