
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.services.n8n_deduplication import N8NDeduplication


@pytest_asyncio.fixture
async def dedup():
    """Create an isolated Redis-backed deduplication instance."""
    redis = Redis.from_url(
        "redis://127.0.0.1:6379/15",
        decode_responses=True,
    )

    await redis.ping()

    instance = N8NDeduplication(redis=redis, ttl_seconds=60)

    yield instance

    try:
        keys = []
        async for key in redis.scan_iter(match="n8n:message:test-dedup-*"):
            keys.append(key)

        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_acquire_new_message(dedup):
    token = await dedup.acquire("test-dedup-001")

    assert isinstance(token, str)
    assert token
    assert await dedup.get_status("test-dedup-001") == "processing"


@pytest.mark.asyncio
async def test_reject_duplicate_message(dedup):
    first = await dedup.acquire("test-dedup-002")
    second = await dedup.acquire("test-dedup-002")

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_begin_send(dedup):
    token = await dedup.acquire("test-dedup-003")

    result = await dedup.begin_send("test-dedup-003", token)

    assert result is True
    assert await dedup.get_status("test-dedup-003") == "sending"


@pytest.mark.asyncio
async def test_complete_message(dedup):
    token = await dedup.acquire("test-dedup-004")
    await dedup.begin_send("test-dedup-004", token)

    result = await dedup.complete("test-dedup-004", token)

    assert result is True
    assert await dedup.get_status("test-dedup-004") == "completed"


@pytest.mark.asyncio
async def test_wrong_token_cannot_complete_message(dedup):
    token = await dedup.acquire("test-dedup-005")
    await dedup.begin_send("test-dedup-005", token)

    result = await dedup.complete(
        "test-dedup-005",
        "invalid-ownership-token",
    )

    assert result is False
    assert await dedup.get_status("test-dedup-005") == "sending"


@pytest.mark.asyncio
async def test_completed_message_cannot_be_reacquired(dedup):
    token = await dedup.acquire("test-dedup-006")
    await dedup.begin_send("test-dedup-006", token)
    await dedup.complete("test-dedup-006", token)

    assert await dedup.acquire("test-dedup-006") is None


@pytest.mark.asyncio
async def test_missing_message_returns_none(dedup):
    assert await dedup.get_status("test-dedup-007") is None


@pytest.mark.asyncio
async def test_invalid_message_id(dedup):
    with pytest.raises(ValueError, match="Message ID is required"):
        await dedup.acquire("")


def test_invalid_ttl():
    with pytest.raises(ValueError, match="TTL must be greater than zero"):
        N8NDeduplication(redis=None, ttl_seconds=0)


@pytest.mark.asyncio
async def test_mark_message_as_unknown(dedup):
    token = await dedup.acquire("test-dedup-008")
    await dedup.begin_send("test-dedup-008", token)

    result = await dedup.mark_unknown("test-dedup-008", token)

    assert result is True
    assert await dedup.get_status("test-dedup-008") == "unknown"


@pytest.mark.asyncio
async def test_unknown_message_cannot_be_reacquired(dedup):
    token = await dedup.acquire("test-dedup-009")
    await dedup.begin_send("test-dedup-009", token)
    await dedup.mark_unknown("test-dedup-009", token)

    assert await dedup.acquire("test-dedup-009") is None


@pytest.mark.asyncio
async def test_wrong_token_cannot_mark_message_as_unknown(dedup):
    token = await dedup.acquire("test-dedup-010")
    await dedup.begin_send("test-dedup-010", token)

    result = await dedup.mark_unknown(
        "test-dedup-010",
        "invalid-ownership-token",
    )

    assert result is False
    assert await dedup.get_status("test-dedup-010") == "sending"


@pytest.mark.asyncio
async def test_cannot_complete_before_begin_send(dedup):
    token = await dedup.acquire("test-dedup-011")

    result = await dedup.complete("test-dedup-011", token)

    assert result is False
    assert await dedup.get_status("test-dedup-011") == "processing"


@pytest.mark.asyncio
async def test_wrong_token_cannot_begin_send(dedup):
    await dedup.acquire("test-dedup-012")

    result = await dedup.begin_send(
        "test-dedup-012",
        "invalid-ownership-token",
    )

    assert result is False
    assert await dedup.get_status("test-dedup-012") == "processing"


@pytest.mark.asyncio
async def test_completed_message_has_no_expiration(dedup):
    token = await dedup.acquire("test-dedup-013")
    await dedup.begin_send("test-dedup-013", token)
    await dedup.complete("test-dedup-013", token)

    ttl = await dedup.redis.ttl("n8n:message:test-dedup-013")

    assert ttl == -1


@pytest.mark.asyncio
async def test_unknown_message_has_no_expiration(dedup):
    token = await dedup.acquire("test-dedup-014")
    await dedup.begin_send("test-dedup-014", token)
    await dedup.mark_unknown("test-dedup-014", token)

    ttl = await dedup.redis.ttl("n8n:message:test-dedup-014")

    assert ttl == -1


@pytest.mark.asyncio
async def test_sending_message_has_no_expiration(dedup):
    token = await dedup.acquire("test-dedup-015")
    await dedup.begin_send("test-dedup-015", token)

    ttl = await dedup.redis.ttl("n8n:message:test-dedup-015")

    assert ttl == -1


@pytest.mark.asyncio
async def test_release_processing_only_removes_owned_claim():
    """Release only the processing claim owned by the correct token."""
    from uuid import uuid4

    from redis.asyncio import Redis

    from app.services.n8n_deduplication import N8NDeduplication

    redis = Redis(
        host="127.0.0.1",
        port=6379,
        db=15,
        decode_responses=True,
    )

    message_id = f"test-release-{uuid4().hex}"

    deduplication = N8NDeduplication(
        redis=redis,
        ttl_seconds=300,
    )

    try:
        token = await deduplication.acquire(message_id)

        assert token is not None
        assert await deduplication.get_status(message_id) == "processing"

        # Another process must not release this claim.
        assert await deduplication.release_processing(
            message_id,
            "incorrect-token",
        ) is False

        assert await deduplication.get_status(message_id) == "processing"

        # The owner can release its processing claim.
        assert await deduplication.release_processing(
            message_id,
            token,
        ) is True

        assert await deduplication.get_status(message_id) is None

        # Releasing an already deleted claim must fail safely.
        assert await deduplication.release_processing(
            message_id,
            token,
        ) is False

    finally:
        await redis.delete(f"n8n:message:{message_id}")
        await redis.aclose()


@pytest.mark.asyncio
async def test_release_processing_preserves_delivery_states():
    """Never release a claim after the sending phase begins."""
    from uuid import uuid4

    from redis.asyncio import Redis

    from app.services.n8n_deduplication import N8NDeduplication

    redis = Redis(
        host="127.0.0.1",
        port=6379,
        db=15,
        decode_responses=True,
    )

    deduplication = N8NDeduplication(redis=redis)

    message_ids = [
        f"test-sending-{uuid4().hex}",
        f"test-completed-{uuid4().hex}",
        f"test-unknown-{uuid4().hex}",
    ]

    try:
        tokens = []

        for message_id in message_ids:
            token = await deduplication.acquire(message_id)
            assert token is not None
            assert await deduplication.begin_send(message_id, token)
            tokens.append(token)

        # Keep a message in the sending state.
        assert await deduplication.release_processing(
            message_ids[0], tokens[0]
        ) is False
        assert await deduplication.get_status(message_ids[0]) == "sending"

        # Keep a completed message.
        assert await deduplication.complete(message_ids[1], tokens[1])
        assert await deduplication.release_processing(
            message_ids[1], tokens[1]
        ) is False
        assert await deduplication.get_status(message_ids[1]) == "completed"

        # Keep a message with an uncertain delivery outcome.
        assert await deduplication.mark_unknown(message_ids[2], tokens[2])
        assert await deduplication.release_processing(
            message_ids[2], tokens[2]
        ) is False
        assert await deduplication.get_status(message_ids[2]) == "unknown"

    finally:
        for message_id in message_ids:
            await redis.delete(f"n8n:message:{message_id}")
        await redis.aclose()
