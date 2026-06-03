from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.bootstrap import PROJECT_DIR, load_environment


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Comma-separated WhatsApp user ids (wa_id). Empty = allow all (dev only).
    allowed_wa_ids: str = ""


@lru_cache
def get_settings() -> Settings:
    load_environment()
    return Settings()
