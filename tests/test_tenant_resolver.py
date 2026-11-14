
from app.config import Settings
from app.services.tenant_resolver import TenantResolver


def test_resolve_known_tenant():
    settings = Settings(
        whatsapp_phone_number_id="123456789",
    )
    resolver = TenantResolver(settings)

    tenant = resolver.resolve("123456789")

    assert tenant is not None
    assert tenant.tenant_id == "default"
    assert tenant.phone_number_id == "123456789"


def test_reject_unknown_tenant():
    settings = Settings(
        whatsapp_phone_number_id="123456789",
    )
    resolver = TenantResolver(settings)

    tenant = resolver.resolve("999999999")

    assert tenant is None


def test_reject_missing_phone_number_id():
    settings = Settings(
        whatsapp_phone_number_id="123456789",
    )
    resolver = TenantResolver(settings)

    assert resolver.resolve(None) is None
    assert resolver.resolve("") is None


def test_reject_unconfigured_channel():
    settings = Settings(
        whatsapp_phone_number_id="",
    )
    resolver = TenantResolver(settings)

    assert resolver.resolve("123456789") is None
