
from app.config import Settings


def should_route_to_n8n(wa_id: str, settings: Settings) -> bool:
    """Determine whether an incoming WhatsApp message should use n8n."""

    if not settings.n8n_enabled:
        return False

    if not wa_id or not wa_id.strip():
        return False

    if not settings.n8n_webhook_url or not settings.n8n_api_key:
        return False

    routing_mode = settings.n8n_routing_mode.strip().lower()

    if routing_mode == "disabled":
        return False

    if routing_mode == "all":
        return True

    if routing_mode == "allowlist":
        allowed_users = {
            user.strip()
            for user in settings.n8n_allowed_wa_ids.split(",")
            if user.strip()
        }

        return wa_id.strip() in allowed_users

    return False
