"""Config loading for external A2A agents (``remote-agents.json``)."""

import json

from harness.core.configuration import RemoteAgentsConfiguration


def test_loads_agents_and_expands_env_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret-123")
    (tmp_path / "remote-agents.json").write_text(json.dumps({
        "agents": {
            "acme": {
                "card_url": "https://agents.acme.com/.well-known/agent-card.json",
                "auth": {"type": "bearer", "token": "${MY_TOKEN}"},
                "allowed_profiles": ["coder"],
                "card_ttl_seconds": 120,
            },
            "disabled-one": {"card_url": "https://x/.well-known/agent-card.json", "enabled": False},
        }
    }))

    configuration = RemoteAgentsConfiguration.from_dotagents_roots([tmp_path])
    assert set(configuration.agents) == {"acme", "disabled-one"}
    assert set(configuration.enabled_agents()) == {"acme"}  # disabled + those without card_url drop out

    acme = configuration.agents["acme"]
    assert acme.auth.type == "bearer"
    assert acme.auth.token == "secret-123"  # ${MY_TOKEN} expanded from the environment
    assert acme.allowed_profiles == ["coder"]
    assert acme.card_ttl_seconds == 120


def test_missing_file_is_empty(tmp_path):
    configuration = RemoteAgentsConfiguration.from_dotagents_roots([tmp_path])
    assert configuration.agents == {}
