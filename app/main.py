import logging
from contextlib import asynccontextmanager

from app.bootstrap import load_environment

load_environment()

from fastapi import FastAPI

from app.webhooks.whatsapp import router as whatsapp_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup: connect to Redis (or fall back to direct handler)."""
    from app.config import get_settings
    from app.webhooks.whatsapp import set_arq_pool, set_fallback_handler

    settings = get_settings()

    if settings.redis_url:
        try:
            from arq.connections import create_pool, RedisSettings
            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            set_arq_pool(pool)
            logger.info("ARQ Redis pool connected — messages will be queued for workers")
            yield
            await pool.close()
            return
        except Exception as exc:
            logger.warning("Redis unavailable (%s); falling back to in-process handler", exc)

    # Dev / no-Redis fallback: run agent in-process (single worker only)
    logger.warning("Running in dev mode — no Redis queue, no shared state across restarts")
    from app.mcp.loader import init_mcp_pool, close_mcp_pool
    from app.agent.tools import get_supplemental_tools
    from app.services.handler import MessageHandler

    mcp_tools = await init_mcp_pool()
    supplemental = get_supplemental_tools()
    handler = MessageHandler(tools=mcp_tools + supplemental)
    set_fallback_handler(handler)

    yield

    await close_mcp_pool()


app = FastAPI(
    title="WhatsApp Cloud Assistant",
    description="Meta webhooks + LangGraph + MCP tools",
    lifespan=lifespan,
)
app.include_router(whatsapp_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
