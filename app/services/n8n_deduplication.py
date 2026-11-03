
import logging
from uuid import uuid4

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class N8NDeduplication:
    """Manage n8n message processing and delivery states using Redis."""

    def __init__(self, redis: Redis, ttl_seconds: int = 300) -> None:
        if ttl_seconds <= 0:
            raise ValueError("TTL must be greater than zero")

        self.redis = redis
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(message_id: str) -> str:
        if not message_id or not message_id.strip():
            raise ValueError("Message ID is required")

        return f"n8n:message:{message_id.strip()}"

    async def acquire(self, message_id: str) -> str | None:
        """Claim a new message atomically with a temporary processing lock."""
        token = uuid4().hex

        acquired = await self.redis.set(
            self._key(message_id),
            f"processing:{token}",
            nx=True,
            ex=self.ttl_seconds,
        )

        if not acquired:
            logger.info("Message already claimed: %s", message_id)
            return None

        return token

    async def _transition(
        self,
        message_id: str,
        token: str,
        expected_status: str,
        new_status: str,
    ) -> bool:
        """Atomically transition only when the owner and status match."""
        if not token:
            raise ValueError("Ownership token is required")

        script = """
        local current = redis.call('GET', KEYS[1])

        if current == ARGV[1] then
            redis.call('SET', KEYS[1], ARGV[2])
            return 1
        end

        return 0
        """

        result = await self.redis.eval(
            script,
            1,
            self._key(message_id),
            f"{expected_status}:{token}",
            new_status,
        )

        return bool(result)

    async def release_processing(
        self,
        message_id: str,
        token: str,
    ) -> bool:
        """Release only a processing claim owned by this token."""
        if not token:
            raise ValueError("Ownership token is required")

        script = """
        local current = redis.call('GET', KEYS[1])

        if current == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end

        return 0
        """

        result = await self.redis.eval(
            script,
            1,
            self._key(message_id),
            f"processing:{token}",
        )

        return bool(result)

    async def begin_send(self, message_id: str, token: str) -> bool:
        """Persist an irreversible send attempt before contacting Meta."""
        return await self._transition(
            message_id,
            token,
            "processing",
            f"sending:{token}",
        )

    async def complete(self, message_id: str, token: str) -> bool:
        """Persist a confirmed send without automatic expiration."""
        return await self._transition(
            message_id,
            token,
            "sending",
            "completed",
        )

    async def mark_unknown(self, message_id: str, token: str) -> bool:
        """Persist an uncertain send without automatic expiration."""
        return await self._transition(
            message_id,
            token,
            "sending",
            "unknown",
        )

    async def get_status(self, message_id: str) -> str | None:
        """Return the current message processing status."""
        value = await self.redis.get(self._key(message_id))

        if value is None:
            return None

        if isinstance(value, bytes):
            value = value.decode("utf-8")

        if value in {"completed", "unknown"}:
            return value

        if value.startswith("processing:"):
            return "processing"

        if value.startswith("sending:"):
            return "sending"

        return "unknown"
