
import httpx
import pytest

from app.services.n8n_client import N8NClient, N8NClientError


@pytest.fixture
def client():
    return N8NClient(
        webhook_url="http://localhost:5678/webhook/whatsapp-python-n8n",
        api_key="test-api-key",
    )


@pytest.mark.asyncio
async def test_generate_reply_success(client, monkeypatch):
    async def mock_post(self, url, **kwargs):
        assert url == client.webhook_url
        assert kwargs["headers"]["X-N8N-API-KEY"] == "test-api-key"
        assert kwargs["json"] == {
            "message_id": "msg-001",
            "wa_id": "50688888888",
            "text": "Hello",
        }
        return httpx.Response(
            200,
            json={"reply": "Hello from n8n"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    reply = await client.generate_reply("msg-001", "50688888888", "Hello")

    assert reply == "Hello from n8n"


@pytest.mark.asyncio
async def test_generate_reply_empty_response(client, monkeypatch):
    async def mock_post(self, url, **kwargs):
        return httpx.Response(
            200,
            json={"reply": ""},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(N8NClientError, match="empty or invalid reply"):
        await client.generate_reply("msg-001", "50688888888", "Hello")


@pytest.mark.asyncio
async def test_generate_reply_http_error(client, monkeypatch):
    async def mock_post(self, url, **kwargs):
        return httpx.Response(
            500,
            json={"error": "Internal server error"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(N8NClientError, match="HTTP request failed"):
        await client.generate_reply("msg-001", "50688888888", "Hello")


@pytest.mark.asyncio
async def test_generate_reply_timeout(client, monkeypatch):
    async def mock_post(self, url, **kwargs):
        raise httpx.ReadTimeout("Request timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(N8NClientError, match="timed out"):
        await client.generate_reply("msg-001", "50688888888", "Hello")


@pytest.mark.asyncio
async def test_generate_reply_invalid_json(client, monkeypatch):
    async def mock_post(self, url, **kwargs):
        return httpx.Response(
            200,
            content=b"not-json",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(N8NClientError, match="invalid JSON"):
        await client.generate_reply("msg-001", "50688888888", "Hello")
