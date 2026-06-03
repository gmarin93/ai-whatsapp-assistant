import logging
from contextlib import asynccontextmanager

from app.bootstrap import load_environment

load_environment()

from fastapi import FastAPI

from app.webhooks.whatsapp import router as whatsapp_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.webhooks.whatsapp import get_handler

    get_handler()
    yield


app = FastAPI(
    title="WhatsApp Cloud Assistant",
    description="Starter: Meta webhooks + LangGraph",
    lifespan=lifespan,
)
app.include_router(whatsapp_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
