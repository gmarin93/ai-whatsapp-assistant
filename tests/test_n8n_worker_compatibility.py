
from unittest.mock import AsyncMock, patch

import pytest
import asyncio
from app.worker import process_message
from arq import Retry

@pytest.mark.asyncio
async def test_legacy_message_processing():
    """Process existing jobs without a message ID."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    handler = AsyncMock()
    ctx = {"redis": redis, "handler": handler}

    await process_message(
        ctx,
        "50688888888",
        "Hello",
    )

    handler.handle_text.assert_awaited_once_with(
        "50688888888",
        "Hello",
    )

    redis.eval.assert_awaited_once()

    args = redis.eval.await_args.args

    assert "redis.call('GET', KEYS[1])" in args[0]
    assert "redis.call('DEL', KEYS[1])" in args[0]
    assert args[1] == 1
    assert args[2] == "whatsapp:lock:50688888888"
    assert isinstance(args[3], str)
    assert args[3]

    redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_message_processing():
    """Process jobs that include a message ID."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    handler = AsyncMock()
    ctx = {"redis": redis, "handler": handler}

    await process_message(
        ctx,
        "50688888888",
        "Hello",
        "wamid.test-001",
    )

    handler.handle_text.assert_awaited_once_with(
        "50688888888",
        "Hello",
    )

    redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_requeue_preserves_message_id():
    """Preserve message identity when the user lock is busy."""
    redis = AsyncMock()
    redis.set.return_value = None

    handler = AsyncMock()
    ctx = {"redis": redis, "handler": handler}

    with patch("app.worker.get_settings") as mock_settings:
        mock_settings.return_value.user_lock_ttl_seconds = 120
        mock_settings.return_value.arq_queue_name = "whatsapp:jobs"

        await process_message(
            ctx,
            "50688888888",
            "Hello",
            "wamid.test-002",
        )

    redis.enqueue_job.assert_awaited_once_with(
        "process_message",
        "50688888888",
        "Hello",
        "wamid.test-002",
        _defer_by=2,
        _queue_name="whatsapp:jobs",
    )

    handler.handle_text.assert_not_awaited()
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_requeue_keeps_compatibility():
    """Preserve compatibility when an old job is requeued."""
    redis = AsyncMock()
    redis.set.return_value = None

    handler = AsyncMock()
    ctx = {"redis": redis, "handler": handler}

    with patch("app.worker.get_settings") as mock_settings:
        mock_settings.return_value.user_lock_ttl_seconds = 120
        mock_settings.return_value.arq_queue_name = "whatsapp:jobs"

        await process_message(
            ctx,
            "50688888888",
            "Hello",
        )

    redis.enqueue_job.assert_awaited_once_with(
        "process_message",
        "50688888888",
        "Hello",
        None,
        _defer_by=2,
        _queue_name="whatsapp:jobs",
    )

    handler.handle_text.assert_not_awaited()
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_lock_is_not_deleted_directly():
    """Never remove a lock without checking ownership."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 0

    handler = AsyncMock()
    ctx = {"redis": redis, "handler": handler}

    await process_message(
        ctx,
        "50688888888",
        "Hello",
        "wamid.test-003",
    )

    handler.handle_text.assert_awaited_once()
    redis.eval.assert_awaited_once()
    redis.delete.assert_not_awaited()

@pytest.mark.asyncio
async def test_worker_cancels_processing_when_redis_lock_is_lost():
    """Cancel message processing when Redis lock ownership is lost."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 0

    handler = AsyncMock()
    handler.handle_text.side_effect = lambda *_: asyncio.sleep(10)



@pytest.mark.asyncio
async def test_n8n_handled_message_skips_original_agent():
    """Prevent the original agent from replying to n8n-owned messages."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    handler = AsyncMock()
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = True

    ctx = {
        "redis": redis,
        "handler": handler,
        "n8n_dispatcher": dispatcher,
    }

    await process_message(
        ctx,
        "50688888888",
        "Hello",
        "wamid.n8n-001",
    )

    dispatcher.dispatch.assert_awaited_once_with(
        message_id="wamid.n8n-001",
        wa_id="50688888888",
        text="Hello",
    )

    handler.handle_text.assert_not_awaited()
    redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_n8n_unhandled_message_uses_original_agent():
    """Preserve the original agent for messages not owned by n8n."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    handler = AsyncMock()
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = False

    ctx = {
        "redis": redis,
        "handler": handler,
        "n8n_dispatcher": dispatcher,
    }

    await process_message(
        ctx,
        "50677777777",
        "Hello",
        "wamid.original-001",
    )

    dispatcher.dispatch.assert_awaited_once_with(
        message_id="wamid.original-001",
        wa_id="50677777777",
        text="Hello",
    )

    handler.handle_text.assert_awaited_once_with(
        "50677777777",
        "Hello",
    )



@pytest.mark.asyncio
async def test_n8n_failure_schedules_retry_when_claim_released():
    """Retry n8n processing when Redis confirms the claim is gone."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    handler = AsyncMock()
    dispatcher = AsyncMock()

    dispatcher.dispatch.side_effect = RuntimeError(
        "n8n processing failed"
    )
    dispatcher.deduplication.get_status.return_value = None

    ctx = {
        "redis": redis,
        "handler": handler,
        "n8n_dispatcher": dispatcher,
        "job_try": 1,
    }

    with pytest.raises(Retry) as exc_info:
        await process_message(
            ctx,
            "50688888888",
            "Hello",
            "wamid.retry-001",
        )

    dispatcher.dispatch.assert_awaited_once_with(
        message_id="wamid.retry-001",
        wa_id="50688888888",
        text="Hello",
    )

    dispatcher.deduplication.get_status.assert_awaited_once_with(
        "wamid.retry-001"
    )

    assert exc_info.value.defer_score == 5000

    handler.handle_text.assert_not_awaited()

    # The user lock must be released even when ARQ requests a retry.
    redis.eval.assert_awaited()


@pytest.mark.asyncio
async def test_n8n_failure_does_not_retry_when_claim_exists():
    """Block automatic retries when Redis still contains the message."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    handler = AsyncMock()
    dispatcher = AsyncMock()

    dispatcher.dispatch.side_effect = RuntimeError(
        "n8n processing failed"
    )
    dispatcher.deduplication.get_status.return_value = "processing"

    ctx = {
        "redis": redis,
        "handler": handler,
        "n8n_dispatcher": dispatcher,
        "job_try": 1,
    }

    with pytest.raises(
        RuntimeError,
        match="n8n processing failed",
    ):
        await process_message(
            ctx,
            "50688888888",
            "Hello",
            "wamid.blocked-001",
        )

    dispatcher.deduplication.get_status.assert_awaited_once_with(
        "wamid.blocked-001"
    )

    handler.handle_text.assert_not_awaited()

    # The user lock must still be released.
    redis.eval.assert_awaited()


@pytest.mark.asyncio
async def test_n8n_failure_does_not_retry_when_redis_status_fails():
    """Do not request a retry if the message state cannot be verified."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    handler = AsyncMock()
    dispatcher = AsyncMock()

    dispatcher.dispatch.side_effect = RuntimeError(
        "n8n processing failed"
    )

    dispatcher.deduplication.get_status.side_effect = ConnectionError(
        "Redis unavailable"
    )

    ctx = {
        "redis": redis,
        "handler": handler,
        "n8n_dispatcher": dispatcher,
        "job_try": 1,
    }

    with pytest.raises(
        RuntimeError,
        match="Cannot safely retry n8n message",
    ):
        await process_message(
            ctx,
            "50688888888",
            "Hello",
            "wamid.redis-failure-001",
        )

    dispatcher.deduplication.get_status.assert_awaited_once_with(
        "wamid.redis-failure-001"
    )

    handler.handle_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_user_retries_when_enqueue_returns_none():
    """Retry the original job when ARQ cannot create a deferred job."""
    from arq import Retry

    redis = AsyncMock()
    redis.set.return_value = None
    redis.enqueue_job.return_value = None

    handler = AsyncMock()
    ctx = {
        "redis": redis,
        "handler": handler,
        "job_try": 1,
    }

    with pytest.raises(Retry) as exc_info:
        await process_message(
            ctx,
            "50688888888",
            "Hello",
            "wamid.busy-001",
        )

    assert exc_info.value.defer_score == 2000

    redis.enqueue_job.assert_awaited_once()
    handler.handle_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_user_retries_when_enqueue_raises():
    """Retry the original job when deferred enqueueing fails."""
    from arq import Retry

    redis = AsyncMock()
    redis.set.return_value = None
    redis.enqueue_job.side_effect = ConnectionError(
        "Redis unavailable"
    )

    handler = AsyncMock()
    ctx = {
        "redis": redis,
        "handler": handler,
        "job_try": 1,
    }

    with pytest.raises(Retry) as exc_info:
        await process_message(
            ctx,
            "50688888888",
            "Hello",
            "wamid.busy-002",
        )

    assert exc_info.value.defer_score == 2000

    redis.enqueue_job.assert_awaited_once()
    handler.handle_text.assert_not_awaited()
