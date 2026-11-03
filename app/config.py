from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.bootstrap import PROJECT_DIR, load_environment


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",

    )
    # Optional n8n integration
    n8n_enabled: bool = False
    n8n_webhook_url: str = ""
    n8n_api_key: str = ""
    n8n_allowed_wa_ids: str = ""
    n8n_timeout_seconds: float = 30.0

    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Speech-to-text model for transcribing WhatsApp voice notes.
    whisper_model: str = "whisper-1"

    # Comma-separated WhatsApp user ids (wa_id). Empty = allow all (dev only).
    allowed_wa_ids: str = ""

    # Redis — used for task queue, conversation checkpoints, and dedup.
    # Leave empty to run without Redis (dev mode: in-memory, no queue).
    redis_url: str = "redis://localhost:6379"

    # ARQ job queue name
    arq_queue_name: str = "whatsapp:jobs"

    # Message dedup TTL in seconds (Meta can re-deliver within ~30 s)
    dedup_ttl_seconds: int = 60

    # Per-user lock TTL — max time a worker holds the lock for one message
    user_lock_ttl_seconds: int = 120

    # Number of stdio subprocesses to open per MCP server. stdio servers are
    # single-threaded, so a pool of N processes allows N concurrent tool calls
    # per worker. Set to 1 to keep a single subprocess per server.
    mcp_pool_size: int = 4

    # LangSmith tracing — https://smith.langchain.com
    # Set langchain_tracing_v2=true to send traces for every LLM call, tool
    # invocation, and graph step.  All other settings are optional.
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "whatsapp-assistant"
    langchain_endpoint: str = "https://api.smith.langchain.com"

    # Daily briefing — comma-separated wa_ids that receive a morning briefing
    # (calendar + World Cup matches + weather). Empty = feature disabled.
    briefing_wa_ids: str = ""
    # Hour (UTC) the briefing cron fires. 13 UTC = 07:00 America/Costa_Rica (UTC-6).
    briefing_cron_hour_utc: int = 13

    # Google Calendar OAuth — paths to the client secrets and cached token.
    # In Docker these are mounted as volumes so a refreshed token persists
    # without rebuilding the image. Leave the token file missing to disable.
    google_credentials_file: str = "credentials.json"
    google_token_file: str = "token.json"


@lru_cache
def get_settings() -> Settings:
    load_environment()
    return Settings()
