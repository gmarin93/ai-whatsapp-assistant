
import json
from unittest.mock import AsyncMock, patch

import pytest
from arq.jobs import JobStatus
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

from app.webhooks import whatsapp


def build_payload(
    message_id: str,
    text: str = "Hello",
    wa_id: str = "50688888888",
) -> dict:
    """Build a simulated Meta WhatsApp webhook payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "test-business-id",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": wa_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }


def build_request(payload: dict) -> Request:
    """Create a simulated incoming WhatsApp request."""
    body = json.dumps(payload).encode("utf-8")

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    return Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/webhooks/whatsapp",
            "headers": [],
            "query_string": b"",
        },
        receive=receive,
    )


async def post_webhook(payload: dict):
    """Execute the actual FastAPI webhook route."""
    app = FastAPI()
    app.include_router(whatsapp.router)

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.post(
            "/webhooks/whatsapp",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )


@pytest.fixture
def arq_pool():
    """Provide an isolated mocked ARQ pool for each test."""
    pool = AsyncMock()

    original_pool = whatsapp._arq_pool
    original_handler = whatsapp._fallback_handler

    whatsapp.set_arq_pool(pool)
    whatsapp.set_fallback_handler(None)

    try:
        yield pool
    finally:
        whatsapp.set_arq_pool(original_pool)
        whatsapp.set_fallback_handler(original_handler)


@pytest.mark.asyncio
async def test_webhook_passes_message_id_to_arq(arq_pool):
    """Verify that Meta message IDs are forwarded to ARQ."""
    message_id = "wamid.test-001"

    arq_pool.set.return_value = True
    arq_pool.enqueue_job.return_value = object()

    payload = build_payload(
        message_id,
        text="Hello from Meta",
    )

    with patch.object(
        whatsapp,
        "_validate_signature",
        return_value=True,
    ):
        response = await post_webhook(payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    arq_pool.set.assert_awaited_once()

    arq_pool.enqueue_job.assert_awaited_once_with(
        "process_message",
        "50688888888",
        "Hello from Meta",
        message_id,
        "",
        _job_id=f"whatsapp:{message_id}",
        _queue_name="whatsapp:jobs",
    )

    arq_pool.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_passes_phone_number_id_to_arq(arq_pool):
    """Forward the receiving WhatsApp channel ID to ARQ."""
    message_id = "wamid.channel-001"
    phone_number_id = "123456789"

    arq_pool.set.return_value = True
    arq_pool.enqueue_job.return_value = object()

    payload = build_payload(
        message_id,
        text="Hello from business channel",
    )

    payload["entry"][0]["changes"][0]["value"]["metadata"] = {
        "phone_number_id": phone_number_id,
    }

    with patch.object(
        whatsapp,
        "_validate_signature",
        return_value=True,
    ):
        response = await post_webhook(payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    arq_pool.enqueue_job.assert_awaited_once_with(
        "process_message",
        "50688888888",
        "Hello from business channel",
        message_id,
        phone_number_id,
        _job_id=f"whatsapp:{message_id}",
        _queue_name="whatsapp:jobs",
    )


@pytest.mark.asyncio
async def test_webhook_preserves_deduplication_when_enqueue_fails(
    arq_pool,
):
    """Preserve the message claim when the enqueue outcome is uncertain."""
    message_id = "wamid.enqueue-failure-001"

    arq_pool.set.return_value = True
    arq_pool.eval.return_value = 1

    arq_pool.enqueue_job.side_effect = RuntimeError(
        "Redis queue unavailable"
    )

    payload = build_payload(
        message_id,
        text="Hello from Meta",
    )

    with (
        patch.object(
            whatsapp,
            "_validate_signature",
            return_value=True,
        ),
        patch.object(
            whatsapp,
            "_get_arq_job_status",
            new_callable=AsyncMock,
            return_value=JobStatus.not_found,
        ) as mock_status,
    ):
        response = await post_webhook(payload)

    assert response.status_code == 503
    assert response.json()["detail"] == "Message queue unavailable"

    arq_pool.enqueue_job.assert_awaited_once()

    mock_status.assert_awaited_once_with(
        message_id,
        "whatsapp:jobs",
    )

    # Keep the claim when the enqueue outcome is uncertain.
    arq_pool.eval.assert_not_awaited()
    arq_pool.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_message_is_not_enqueued(arq_pool):
    """Skip duplicate messages when ARQ confirms the job exists."""
    message_id = "wamid.duplicate-001"

    arq_pool.set.return_value = None

    payload = build_payload(
        message_id,
        text="Hello again",
    )

    with (
        patch.object(
            whatsapp,
            "_validate_signature",
            return_value=True,
        ),
        patch.object(
            whatsapp,
            "_get_arq_job_status",
            new_callable=AsyncMock,
            return_value=JobStatus.queued,
        ) as mock_status,
    ):
        response = await post_webhook(payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    mock_status.assert_awaited_once_with(
        message_id,
        "whatsapp:jobs",
    )

    arq_pool.set.assert_awaited_once()
    arq_pool.enqueue_job.assert_not_awaited()
    arq_pool.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_uses_deterministic_job_id(arq_pool):
    """Verify that the webhook creates a stable ARQ job ID."""
    message_id = "wamid.test-stable-job-001"
    wa_id = "50688888888"

    arq_pool.set.return_value = True
    arq_pool.enqueue_job.return_value = object()

    settings = whatsapp.get_settings()

    request = build_request(
        build_payload(message_id, wa_id=wa_id)
    )

    with (
        patch.object(
            whatsapp,
            "get_settings",
            return_value=settings,
        ),
        patch.object(
            whatsapp,
            "_validate_signature",
            return_value=True,
        ),
    ):
        result = await whatsapp.receive_webhook(request)

    assert result == {"status": "ok"}

    arq_pool.enqueue_job.assert_awaited_once_with(
        "process_message",
        wa_id,
        "Hello",
        message_id,
        "",
        _job_id=f"whatsapp:{message_id}",
        _queue_name=settings.arq_queue_name,
    )


@pytest.mark.asyncio
async def test_existing_arq_job_does_not_return_error(arq_pool):
    """An existing ARQ job should not cause a webhook failure."""
    message_id = "wamid.existing-job-001"

    arq_pool.set.return_value = True
    arq_pool.enqueue_job.return_value = None

    settings = whatsapp.get_settings()

    request = build_request(build_payload(message_id))

    with (
        patch.object(
            whatsapp,
            "get_settings",
            return_value=settings,
        ),
        patch.object(
            whatsapp,
            "_validate_signature",
            return_value=True,
        ),
    ):
        result = await whatsapp.receive_webhook(request)

    assert result == {"status": "ok"}

    arq_pool.enqueue_job.assert_awaited_once_with(
        "process_message",
        "50688888888",
        "Hello",
        message_id,
        "",
        _job_id=f"whatsapp:{message_id}",
        _queue_name=settings.arq_queue_name,
    )

    # Do not release the claim for an existing ARQ job.
    arq_pool.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_arq_connection_failure_returns_503(arq_pool):
    """Return 503 when enqueue fails and the job is not found."""
    message_id = "wamid.connection-failure-001"

    arq_pool.set.return_value = True
    arq_pool.eval.return_value = 1

    arq_pool.enqueue_job.side_effect = ConnectionError(
        "Connection lost after enqueue attempt"
    )

    settings = whatsapp.get_settings()

    request = build_request(build_payload(message_id))

    with (
        patch.object(
            whatsapp,
            "get_settings",
            return_value=settings,
        ),
        patch.object(
            whatsapp,
            "_validate_signature",
            return_value=True,
        ),
        patch.object(
            whatsapp,
            "_get_arq_job_status",
            new_callable=AsyncMock,
            return_value=JobStatus.not_found,
        ) as mock_status,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await whatsapp.receive_webhook(request)

    assert exc_info.value.status_code == 503

    mock_status.assert_awaited_once_with(
        message_id,
        settings.arq_queue_name,
    )

    arq_pool.enqueue_job.assert_awaited_once()
    arq_pool.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_arq_job_status_when_queued(arq_pool):
    """Verify that an existing ARQ job is detected."""
    message_id = "wamid.status-check-001"
    queue_name = "whatsapp:jobs"

    with patch.object(whatsapp, "Job") as mock_job:
        mock_job.return_value.status = AsyncMock(
            return_value=JobStatus.queued
        )

        result = await whatsapp._get_arq_job_status(
            message_id,
            queue_name,
        )

        assert result == JobStatus.queued

        mock_job.assert_called_once_with(
            f"whatsapp:{message_id}",
            arq_pool,
            _queue_name=queue_name,
        )


@pytest.mark.asyncio
async def test_arq_job_status_when_not_found(arq_pool):
    """Return not_found when ARQ has no job record."""
    with patch.object(whatsapp, "Job") as mock_job:
        mock_job.return_value.status = AsyncMock(
            return_value=JobStatus.not_found
        )

        result = await whatsapp._get_arq_job_status(
            "wamid.not-found-001",
            "whatsapp:jobs",
        )

    assert result == JobStatus.not_found


@pytest.mark.asyncio
async def test_arq_job_status_when_redis_fails(arq_pool):
    """Return None when the ARQ status cannot be determined."""
    with patch.object(whatsapp, "Job") as mock_job:
        mock_job.return_value.status = AsyncMock(
            side_effect=ConnectionError("Redis unavailable")
        )

        result = await whatsapp._get_arq_job_status(
            "wamid.redis-error-001",
            "whatsapp:jobs",
        )

    assert result is None


@pytest.mark.asyncio
async def test_enqueue_failure_with_existing_job_keeps_claim(
    arq_pool,
):
    """Keep the claim when ARQ accepted the job before raising."""
    message_id = "wamid.uncertain-existing-001"

    arq_pool.set.return_value = True

    arq_pool.enqueue_job.side_effect = ConnectionError(
        "Connection lost after enqueue"
    )

    settings = whatsapp.get_settings()

    request = build_request(build_payload(message_id))

    with (
        patch.object(
            whatsapp,
            "get_settings",
            return_value=settings,
        ),
        patch.object(
            whatsapp,
            "_validate_signature",
            return_value=True,
        ),
        patch.object(
            whatsapp,
            "_get_arq_job_status",
            new_callable=AsyncMock,
            return_value=JobStatus.queued,
        ) as mock_status,
    ):
        result = await whatsapp.receive_webhook(request)

    assert result == {"status": "ok"}

    mock_status.assert_awaited_once_with(
        message_id,
        settings.arq_queue_name,
    )

    arq_pool.enqueue_job.assert_awaited_once()

    # Keep the claim when ARQ confirms that the job exists.
    arq_pool.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_claim_with_unknown_job_status_returns_503(
    arq_pool,
):
    """Preserve the claim when an existing job cannot be confirmed."""
    message_id = "wamid.unknown-claim-001"

    arq_pool.set.return_value = None

    payload = build_payload(
        message_id,
        text="Hello again",
    )

    with (
        patch.object(
            whatsapp,
            "_validate_signature",
            return_value=True,
        ),
        patch.object(
            whatsapp,
            "_get_arq_job_status",
            new_callable=AsyncMock,
            return_value=JobStatus.not_found,
        ) as mock_status,
    ):
        response = await post_webhook(payload)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Message processing status uncertain"
    )

    mock_status.assert_awaited_once_with(
        message_id,
        "whatsapp:jobs",
    )

    arq_pool.enqueue_job.assert_not_awaited()
    arq_pool.eval.assert_not_awaited()
