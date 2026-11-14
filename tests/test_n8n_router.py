
from app.config import Settings
from app.services.n8n_router import should_route_to_n8n


def make_settings(**overrides) -> Settings:
    """Create isolated settings without loading local credentials."""
    values = {
        "n8n_enabled": True,
        "n8n_routing_mode": "allowlist",
        "n8n_webhook_url": "http://localhost:5678/webhook/test",
        "n8n_api_key": "test-secret",
        "n8n_allowed_wa_ids": "50688888888",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)



def test_disabled_integration():
    settings = make_settings(n8n_enabled=False)
    assert not should_route_to_n8n("50688888888", settings)


def test_empty_allowed_users():
    settings = make_settings(n8n_allowed_wa_ids="")
    assert not should_route_to_n8n("50688888888", settings)


def test_authorized_user():
    settings = make_settings()
    assert should_route_to_n8n("50688888888", settings)


def test_unauthorized_user():
    settings = make_settings()
    assert not should_route_to_n8n("50677777777", settings)


def test_missing_api_key():
    settings = make_settings(n8n_api_key="")
    assert not should_route_to_n8n("50688888888", settings)


def test_missing_webhook_url():
    settings = make_settings(n8n_webhook_url="")
    assert not should_route_to_n8n("50688888888", settings)


def test_empty_sender():
    settings = make_settings()
    assert not should_route_to_n8n("", settings)


def test_multiple_allowed_users():
    settings = make_settings(
        n8n_allowed_wa_ids="50611111111, 50688888888"
    )
    assert should_route_to_n8n("50688888888", settings)

def test_routing_mode_all_accepts_unknown_user():
    settings = Settings(
        n8n_enabled=True,
        n8n_routing_mode="all",
        n8n_webhook_url="http://localhost:5678/webhook/test",
        n8n_api_key="test-key",
        n8n_allowed_wa_ids="",
    )

    assert should_route_to_n8n("50699999999", settings) is True


def test_routing_mode_disabled_rejects_user():
    settings = Settings(
        n8n_enabled=True,
        n8n_routing_mode="disabled",
        n8n_webhook_url="http://localhost:5678/webhook/test",
        n8n_api_key="test-key",
    )

    assert should_route_to_n8n("50699999999", settings) is False


def test_routing_mode_unknown_rejects_user():
    settings = Settings(
        n8n_enabled=True,
        n8n_routing_mode="invalid",
        n8n_webhook_url="http://localhost:5678/webhook/test",
        n8n_api_key="test-key",
    )

    assert should_route_to_n8n("50699999999", settings) is False
