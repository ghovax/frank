"""AgentCard completeness and inbound-auth configuration."""

from harness.core.a2a_executor import build_agent_card
from harness.core.configuration import A2AServerConfiguration, AgentConfiguration


def test_auth_disabled_by_default():
    configuration = A2AServerConfiguration()
    assert not configuration.enabled()
    assert configuration.card_security() == (None, None)


def test_api_key_security():
    configuration = A2AServerConfiguration(api_key="secret", api_key_header="X-Key")
    assert configuration.enabled()
    schemes, security = configuration.card_security()
    assert schemes["apiKey"] == {"type": "apiKey", "in": "header", "name": "X-Key"}
    assert security == [{"apiKey": []}]


def test_oauth2_security():
    configuration = A2AServerConfiguration(oauth2_jwks_url="https://issuer/.well-known/jwks.json")
    schemes, security = configuration.card_security()
    assert schemes["bearer"]["scheme"] == "bearer"
    assert security == [{"bearer": []}]


def test_card_advertises_capabilities_and_provider():
    card = build_agent_card(AgentConfiguration(name="tester"), [], "http://localhost:8822")
    assert card.capabilities.streaming is True
    assert card.capabilities.push_notifications is True
    assert card.capabilities.state_transition_history is True
    assert card.capabilities.extensions and card.capabilities.extensions[0].uri == "urn:daisy:ext:turn:v1"
    assert card.provider is not None and card.provider.organization == "Daisy"
    assert card.additional_interfaces and card.additional_interfaces[0].transport == "JSONRPC"
    assert card.security_schemes is None  # no auth by default


def test_card_carries_security_when_enabled():
    schemes, security = A2AServerConfiguration(api_key="k").card_security()
    card = build_agent_card(
        AgentConfiguration(name="tester"), [], "http://localhost:8822",
        security_schemes=schemes, security=security,
    )
    assert card.security_schemes is not None and "apiKey" in card.security_schemes
    assert card.security == [{"apiKey": []}]
