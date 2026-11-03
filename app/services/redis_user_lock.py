
import asyncio
import logging
from uuid import uuid4

logger = logging.getLogger(__name__)


class RedisUserLock:
    """Manage ownership-protected Redis locks with TTL renewal."""

    def __init__(
        self,
        redis,
        wa_id: str,
        ttl_seconds: int = 120,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Lock TTL must be greater than zero")

        if not wa_id or not wa_id.strip():
            raise ValueError("WhatsApp user ID is required")

        self.redis = redis
        self.key = f"whatsapp:lock:{wa_id}"
        self.ttl_ms = ttl_seconds * 1000
        self.token = uuid4().hex

        self._renewal_task: asyncio.Task | None = None
        self._owner_task: asyncio.Task | None = None
        self._lost = False

    async def acquire(self) -> bool:
        """Acquire a lock that belongs to this instance."""
        acquired = await self.redis.set(
            self.key,
            self.token,
            px=self.ttl_ms,
            nx=True,
        )
        return bool(acquired)

    async def renew(self) -> bool:
        """Extend the TTL only when this instance owns the lock."""
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('PEXPIRE', KEYS[1], ARGV[2])
        end
        return 0
        """

        result = await self.redis.eval(
            script,
            1,
            self.key,
            self.token,
            self.ttl_ms,
        )
        return bool(result)

    async def release(self) -> bool:
        """Delete the lock only when the ownership token matches."""
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """

        result = await self.redis.eval(
            script,
            1,
            self.key,
            self.token,
        )
        return bool(result)

    async def _renew_loop(self) -> None:
        """Renew the lock and cancel processing if ownership is lost."""
        interval = max(1.0, self.ttl_ms / 3000)

        while True:
            await asyncio.sleep(interval)

            try:
                renewed = await self.renew()
            except Exception:
                logger.exception(
                    "Failed to renew Redis lock: %s",
                    self.key,
                )
                renewed = False

            if not renewed:
                self._lost = True

                logger.error(
                    "Redis lock ownership lost: %s",
                    self.key,
                )

                if self._owner_task is not None:
                    self._owner_task.cancel()

                return

    def start_renewal(self) -> None:
        """Start renewing and monitor the current processing task."""
        if self._renewal_task is not None:
            raise RuntimeError("Lock renewal already started")

        self._owner_task = asyncio.current_task()

        if self._owner_task is None:
            raise RuntimeError("No active processing task")

        self._renewal_task = asyncio.create_task(
            self._renew_loop()
        )

    async def stop_renewal(self) -> None:
        """Stop the renewal task without deleting the lock."""
        task = self._renewal_task

        if task is None:
            return

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._renewal_task = None
            self._owner_task = None

    @property
    def lost(self) -> bool:
        """Report whether lock renewal lost ownership."""
        return self._lost
