
from app.config import Settings


def should_route_to_n8n(wa_id: str, settings: Settings) -> bool:
    """Return True only when n8n is enabled and the sender is authorized."""

    if not settings.n8n_enabled:
        return False

    if not wa_id or not wa_id.strip():
        return False

    if not settings.n8n_webhook_url or not settings.n8n_api_key:
        return False

    allowed_users = {
        user.strip()
        for user in settings.n8n_allowed_wa_ids.split(",")
        if user.strip()
    }

    if not allowed_users:
        return False

    return wa_id.strip() in allowed_users
