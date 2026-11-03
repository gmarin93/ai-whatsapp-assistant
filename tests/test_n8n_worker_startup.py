
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker import startup


@pytest.mark.asyncio
async def test_startup_with_n8n_disabled():
    """Keep the original agent when n8n is disabled."""
    ctx = {"redis": AsyncMock()}

    settings = MagicMock()
    settings.n8n_enabled = False

    with (
        patch("app.worker.get_settings", return_value=settings),
        patch(
            "app.mcp.loader.init_mcp_pool",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.agent.tools.get_supplemental_tools",
            return_value=[],
        ),
        patch("app.services.handler.MessageHandler") as handler_class,
    ):
        await startup(ctx)

    assert ctx["handler"] is handler_class.return_value
    assert ctx["n8n_dispatcher"] is None


@pytest.mark.asyncio
async def test_startup_with_n8n_enabled():
    """Initialize n8n using the existing Redis and Meta clients."""
    redis = AsyncMock()
    ctx = {"redis": redis}

    settings = MagicMock()
    settings.n8n_enabled = True
    settings.n8n_webhook_url = "http://localhost:5678/webhook/test"
    settings.n8n_api_key = "test-secret"
    settings.n8n_timeout_seconds = 30
    settings.n8n_allowed_wa_ids = "50688888888"

    with (
        patch("app.worker.get_settings", return_value=settings),
        patch(
            "app.mcp.loader.init_mcp_pool",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.agent.tools.get_supplemental_tools",
            return_value=[],
        ),
        patch("app.services.handler.MessageHandler"),
        patch("app.services.n8n_client.N8NClient") as n8n_client_class,
        patch(
            "app.services.n8n_deduplication.N8NDeduplication"
        ) as deduplication_class,
        patch(
            "app.services.n8n_dispatcher.N8NDispatcher"
        ) as dispatcher_class,
        patch("app.meta.client.WhatsAppClient") as whatsapp_client_class,
    ):
        await startup(ctx)

        n8n_client_class.assert_called_once_with(
            webhook_url=settings.n8n_webhook_url,
            api_key=settings.n8n_api_key,
            timeout=settings.n8n_timeout_seconds,
        )

        deduplication_class.assert_called_once_with(
            redis=redis,
            ttl_seconds=300,
        )

        dispatcher_class.assert_called_once_with(
            settings=settings,
            n8n_client=n8n_client_class.return_value,
            send_reply=whatsapp_client_class.return_value.send_text,
            deduplication=deduplication_class.return_value,
        )

        assert ctx["n8n_dispatcher"] is dispatcher_class.return_value
