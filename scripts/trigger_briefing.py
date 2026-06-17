"""Trigger the daily briefing now, without waiting for the 07:00 cron.

Enqueues the ``daily_briefing`` job; a running worker picks it up and sends the
briefing to every wa_id in BRIEFING_WA_IDS. Run it against the worker:

    docker compose exec worker python scripts/trigger_briefing.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.bootstrap import load_environment  # noqa: E402

load_environment()


async def main() -> int:
    from arq.connections import RedisSettings, create_pool

    from app.config import get_settings

    settings = get_settings()
    if not settings.redis_url:
        print("ERROR: REDIS_URL not set — the briefing needs the worker queue.", file=sys.stderr)
        return 1
    if not settings.briefing_wa_ids.strip():
        print("WARNING: BRIEFING_WA_IDS is empty — the job will run but send nothing.")

    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job("daily_briefing", _queue_name=settings.arq_queue_name)
    finally:
        await pool.close()
    print("Enqueued daily_briefing — check the worker logs / your WhatsApp.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
