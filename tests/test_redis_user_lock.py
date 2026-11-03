
from unittest.mock import AsyncMock

import pytest

from app.services.redis_user_lock import RedisUserLock


@pytest.mark.asyncio
async def test_acquire_lock():
    """Acquire a lock with a unique ownership token."""
    redis = AsyncMock()
    redis.set.return_value = True

    lock = RedisUserLock(redis, "50688888888", ttl_seconds=120)

    result = await lock.acquire()

    assert result is True
    redis.set.assert_awaited_once_with(
        "whatsapp:lock:50688888888",
        lock.token,
        px=120000,
        nx=True,
    )


@pytest.mark.asyncio
async def test_acquire_busy_lock():
    """Reject acquisition when another worker owns the lock."""
    redis = AsyncMock()
    redis.set.return_value = None

    lock = RedisUserLock(redis, "50688888888")

    assert await lock.acquire() is False


@pytest.mark.asyncio
async def test_renew_owned_lock():
    """Renew the TTL when the ownership token matches."""
    redis = AsyncMock()
    redis.eval.return_value = 1

    lock = RedisUserLock(redis, "50688888888", ttl_seconds=120)

    assert await lock.renew() is True

    args = redis.eval.await_args.args
    assert "PEXPIRE" in args[0]
    assert args[1] == 1
    assert args[2] == lock.key
    assert args[3] == lock.token
    assert args[4] == 120000


@pytest.mark.asyncio
async def test_renew_lost_lock():
    """Reject renewal when ownership has changed."""
    redis = AsyncMock()
    redis.eval.return_value = 0

    lock = RedisUserLock(redis, "50688888888")

    assert await lock.renew() is False


@pytest.mark.asyncio
async def test_release_owned_lock():
    """Delete a lock only when the ownership token matches."""
    redis = AsyncMock()
    redis.eval.return_value = 1

    lock = RedisUserLock(redis, "50688888888")

    assert await lock.release() is True

    args = redis.eval.await_args.args
    assert "redis.call('GET', KEYS[1])" in args[0]
    assert "redis.call('DEL', KEYS[1])" in args[0]
    assert args[2] == lock.key
    assert args[3] == lock.token

    redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_unowned_lock():
    """Never delete a lock owned by another worker."""
    redis = AsyncMock()
    redis.eval.return_value = 0

    lock = RedisUserLock(redis, "50688888888")

    assert await lock.release() is False
    redis.delete.assert_not_awaited()


def test_invalid_lock_ttl():
    """Reject invalid lock duration."""
    with pytest.raises(ValueError, match="Lock TTL must be greater than zero"):
        RedisUserLock(AsyncMock(), "50688888888", ttl_seconds=0)


def test_invalid_whatsapp_user_id():
    """Reject empty WhatsApp user identifiers."""
    with pytest.raises(ValueError, match="WhatsApp user ID is required"):
        RedisUserLock(AsyncMock(), "   ")

import asyncio


@pytest.mark.asyncio
async def test_automatic_renewal():
    """Verify that background renewal extends the lock TTL."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 1

    lock = RedisUserLock(redis, "50688888888", ttl_seconds=1)

    assert await lock.acquire() is True

    lock.start_renewal()

    try:
        await asyncio.sleep(1.2)
        assert redis.eval.await_count >= 1
        assert lock.lost is False
    finally:
        await lock.stop_renewal()



@pytest.mark.asyncio
async def test_automatic_renewal_detects_lost_lock():
    """Cancel the processing task when Redis lock ownership is lost."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.return_value = 0

    lock = RedisUserLock(
        redis,
        "50688888888",
        ttl_seconds=1,
    )

    assert await lock.acquire() is True

    lock.start_renewal()

    try:
        # Losing ownership must cancel the current processing task.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(1.2)

        assert lock.lost is True
        redis.eval.assert_awaited()

    finally:
        await lock.stop_renewal()
