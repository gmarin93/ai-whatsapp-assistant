
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.services.n8n_dispatcher import N8NDispatcher


def make_settings(**overrides):
    """Create isolated settings for dispatcher tests."""
    values = {
        "n8n_enabled": True,
        "n8n_webhook_url": "http://localhost:5678/webhook/test",
        "n8n_api_key": "test-secret",
        "n8n_allowed_wa_ids": "50688888888",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_dispatcher(settings=None, with_deduplication=True):
    """Create a dispatcher with mocked dependencies."""
    client = AsyncMock()
    client.generate_reply.return_value = "Hello from n8n"

    send_reply = AsyncMock()
    send_reply.return_value = {
        "messaging_product": "whatsapp",
        "messages": [
            {"id": "wamid.test-meta-response-001"}
        ],
    }

    deduplication = AsyncMock() if with_deduplication else None

    if deduplication is not None:
        deduplication.acquire.return_value = "owner-token"
        deduplication.release_processing.return_value = True
        deduplication.begin_send.return_value = True
        deduplication.complete.return_value = True
        deduplication.mark_unknown.return_value = True

    dispatcher = N8NDispatcher(
        settings=settings if settings is not None else make_settings(),
        n8n_client=client,
        send_reply=send_reply,
        deduplication=deduplication,
    )

    return dispatcher, client, send_reply, deduplication


@pytest.mark.asyncio
async def test_disabled_integration():
    dispatcher, client, send_reply, dedup = make_dispatcher(
        make_settings(n8n_enabled=False)
    )

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is False

    client.generate_reply.assert_not_awaited()
    send_reply.assert_not_awaited()
    dedup.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_user():
    dispatcher, client, send_reply, dedup = make_dispatcher()

    assert await dispatcher.dispatch(
        "msg-001", "50677777777", "Hello"
    ) is False

    client.generate_reply.assert_not_awaited()
    send_reply.assert_not_awaited()
    dedup.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_user():
    dispatcher, client, send_reply, dedup = make_dispatcher()

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is True

    client.generate_reply.assert_awaited_once_with(
        message_id="msg-001",
        wa_id="50688888888",
        text="Hello",
    )

    dedup.begin_send.assert_awaited_once_with(
        "msg-001", "owner-token"
    )

    send_reply.assert_awaited_once_with(
        "50688888888",
        "Hello from n8n",
    )

    dedup.complete.assert_awaited_once_with(
        "msg-001", "owner-token"
    )


@pytest.mark.asyncio
async def test_n8n_failure_does_not_send_reply():
    """Propagate an n8n failure without sending a WhatsApp message."""
    dispatcher, client, send_reply, dedup = make_dispatcher()

    client.generate_reply.side_effect = RuntimeError(
        "n8n unavailable"
    )

    with pytest.raises(
        RuntimeError,
        match="n8n processing failed",
    ):
        await dispatcher.dispatch(
            "msg-001",
            "50688888888",
            "Hello",
        )

    dedup.release_processing.assert_awaited_once_with(
        "msg-001", "owner-token"
    )

    send_reply.assert_not_awaited()
    dedup.begin_send.assert_not_awaited()
    dedup.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_whatsapp_send_failure_is_not_forwarded():
    """Keep an uncertain Meta delivery from being retried automatically."""
    dispatcher, client, send_reply, dedup = make_dispatcher()

    send_reply.side_effect = RuntimeError(
        "Meta unavailable"
    )

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is True

    dedup.begin_send.assert_awaited_once()

    dedup.mark_unknown.assert_awaited_once_with(
        "msg-001", "owner-token"
    )

    dedup.complete.assert_not_awaited()
    dedup.release_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_acquires_and_completes_message():
    dispatcher, client, send_reply, dedup = make_dispatcher()

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is True

    dedup.acquire.assert_awaited_once_with("msg-001")

    dedup.begin_send.assert_awaited_once_with(
        "msg-001", "owner-token"
    )

    send_reply.assert_awaited_once()

    dedup.complete.assert_awaited_once_with(
        "msg-001", "owner-token"
    )

    dedup.release_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_message_is_skipped():
    dispatcher, client, send_reply, dedup = make_dispatcher()

    dedup.acquire.return_value = None

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is True

    client.generate_reply.assert_not_awaited()
    send_reply.assert_not_awaited()
    dedup.begin_send.assert_not_awaited()
    dedup.release_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_n8n_failure_does_not_complete_message():
    """Do not complete a message when n8n fails before delivery."""
    dispatcher, client, send_reply, dedup = make_dispatcher()

    client.generate_reply.side_effect = RuntimeError(
        "n8n unavailable"
    )

    with pytest.raises(
        RuntimeError,
        match="n8n processing failed",
    ):
        await dispatcher.dispatch(
            "msg-001",
            "50688888888",
            "Hello",
        )

    dedup.release_processing.assert_awaited_once_with(
        "msg-001", "owner-token"
    )

    dedup.complete.assert_not_awaited()
    dedup.mark_unknown.assert_not_awaited()
    send_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_does_not_complete_message():
    dispatcher, client, send_reply, dedup = make_dispatcher()

    send_reply.side_effect = RuntimeError(
        "Meta unavailable"
    )

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is True

    dedup.complete.assert_not_awaited()
    dedup.mark_unknown.assert_awaited_once()
    dedup.release_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_marks_message_as_unknown():
    dispatcher, client, send_reply, dedup = make_dispatcher()

    send_reply.side_effect = RuntimeError(
        "Meta unavailable"
    )

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is True

    dedup.mark_unknown.assert_awaited_once_with(
        "msg-001", "owner-token"
    )

    dedup.release_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_n8n_failure_does_not_mark_message_as_unknown():
    """An n8n failure occurs before any Meta send attempt."""
    dispatcher, client, send_reply, dedup = make_dispatcher()

    client.generate_reply.side_effect = RuntimeError(
        "n8n unavailable"
    )

    with pytest.raises(
        RuntimeError,
        match="n8n processing failed",
    ):
        await dispatcher.dispatch(
            "msg-001",
            "50688888888",
            "Hello",
        )

    dedup.release_processing.assert_awaited_once_with(
        "msg-001", "owner-token"
    )

    dedup.mark_unknown.assert_not_awaited()
    dedup.begin_send.assert_not_awaited()
    send_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_deduplication_blocks_dispatch():
    """Never send a live message without Redis protection."""
    dispatcher, client, send_reply, _ = make_dispatcher(
        with_deduplication=False
    )

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is True

    client.generate_reply.assert_not_awaited()
    send_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_begin_send_failure_blocks_delivery():
    """Do not send when the sending state cannot be persisted."""
    dispatcher, client, send_reply, dedup = make_dispatcher()

    dedup.begin_send.return_value = False

    assert await dispatcher.dispatch(
        "msg-001", "50688888888", "Hello"
    ) is True

    send_reply.assert_not_awaited()
    dedup.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_message_id_blocks_dispatch():
    """Reject messages without a stable identifier."""
    dispatcher, client, send_reply, dedup = make_dispatcher()

    assert await dispatcher.dispatch(
        "", "50688888888", "Hello"
    ) is True

    client.generate_reply.assert_not_awaited()
    send_reply.assert_not_awaited()
    dedup.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_meta_response_marks_message_as_unknown():
    """Do not mark delivery completed without Meta message acceptance."""
    dispatcher, client, send_reply, dedup = make_dispatcher()

    send_reply.return_value = {
        "messaging_product": "whatsapp",
        "messages": [],
    }

    result = await dispatcher.dispatch(
        "msg-invalid-meta-001",
        "50688888888",
        "Hello",
    )

    assert result is True

    dedup.begin_send.assert_awaited_once_with(
        "msg-invalid-meta-001",
        "owner-token",
    )

    dedup.mark_unknown.assert_awaited_once_with(
        "msg-invalid-meta-001",
        "owner-token",
    )

    dedup.complete.assert_not_awaited()
    dedup.release_processing.assert_not_awaited()
