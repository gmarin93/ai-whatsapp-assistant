
from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class Tenant:
    """Represent a business associated with a WhatsApp channel."""

    tenant_id: str
    phone_number_id: str


class TenantResolver:
    """Resolve a business from an incoming WhatsApp phone number ID."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def resolve(self, phone_number_id: str | None) -> Tenant | None:
        """Return the configured tenant or reject an unknown channel."""

        if not phone_number_id:
            return None

        configured_id = self.settings.whatsapp_phone_number_id.strip()

        if not configured_id:
            return None

        if phone_number_id.strip() != configured_id:
            return None

        return Tenant(
            tenant_id="default",
            phone_number_id=configured_id,
        )
