"""Named logical-leaf bindings stay private and fail closed at startup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apx_agent import (
    Agent,
    AgentCard,
    AgentConfig,
    AgentContext,
    BaseAgent,
    HandoffAgent,
    LoopAgent,
    SequentialAgent,
    finalize_agent,
)
from apx_agent._topology import build_topology


_CARD_URL = "https://pricing.example.com/.well-known/agent.json"


def _config(reference: str = "$PRICING_APP_URL") -> AgentConfig:
    return AgentConfig(
        name="research-assistant",
        bindings={"pricing": reference},
    )


def _finalize(agent: BaseAgent, config: AgentConfig, tmp_path: Path) -> None:
    finalize_agent(agent, config, pyproject_path=str(tmp_path) + "/missing.toml")


def test_finalization_resolves_binding_once_without_replacing_logical_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PRICING_APP_URL", _CARD_URL)
    pricing = Agent(name="pricing", description="Produces an approved price.")
    root = SequentialAgent([Agent(name="data"), pricing], name="review")
    config = _config()

    _finalize(root, config, tmp_path)
    first = root._apx_remote_leaf_bindings
    _finalize(root, config, tmp_path)

    assert root._apx_remote_leaf_bindings is first
    assert first["pricing"].logical_name == "pricing"
    assert first["pricing"].card_url == _CARD_URL
    assert pricing._name == "pricing"
    with pytest.raises(TypeError):
        first["pricing"] = first["pricing"]


def test_local_http_card_url_is_valid(tmp_path: Path) -> None:
    pricing = Agent(name="pricing")
    root = SequentialAgent([pricing], name="review")

    _finalize(
        root,
        _config("http://pricing.local/.well-known/agent.json"),
        tmp_path,
    )

    assert (
        root._apx_remote_leaf_bindings["pricing"].card_url
        == "http://pricing.local/.well-known/agent.json"
    )


@pytest.mark.parametrize("reference", ["$UNSET_PRICING_APP_URL", "   "])
def test_blank_resolved_binding_fails_closed(
    reference: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("UNSET_PRICING_APP_URL", raising=False)
    root = SequentialAgent([Agent(name="pricing")], name="review")

    with pytest.raises(ValueError, match="resolved to a blank A2A card URL"):
        _finalize(root, _config(reference), tmp_path)


@pytest.mark.parametrize(
    "reference",
    ["not-a-url", "ftp://pricing.example.com/.well-known/agent.json"],
)
def test_malformed_card_url_fails_closed(
    reference: str,
    tmp_path: Path,
) -> None:
    root = SequentialAgent([Agent(name="pricing")], name="review")

    with pytest.raises(ValueError, match="malformed A2A card URL"):
        _finalize(root, _config(reference), tmp_path)


def test_unknown_binding_name_fails_closed(tmp_path: Path) -> None:
    root = SequentialAgent([Agent(name="data")], name="review")

    with pytest.raises(ValueError, match="binding 'pricing'.*found 0"):
        _finalize(root, _config(_CARD_URL), tmp_path)


def test_duplicate_logical_leaf_name_fails_closed(tmp_path: Path) -> None:
    root = SequentialAgent(
        [Agent(name="pricing"), Agent(name="pricing")],
        name="review",
    )

    with pytest.raises(ValueError, match="binding 'pricing'.*found 2"):
        _finalize(root, _config(_CARD_URL), tmp_path)


def test_remote_loop_body_requires_control_protocol(tmp_path: Path) -> None:
    pricing = Agent(name="pricing")

    with pytest.raises(
        ValueError,
        match="remote loop completion requires an A2A control protocol",
    ):
        _finalize(LoopAgent(pricing), _config(_CARD_URL), tmp_path)


def test_remote_handoff_peer_requires_control_protocol(tmp_path: Path) -> None:
    triage = Agent(name="triage")
    pricing = Agent(name="pricing")

    with pytest.raises(
        ValueError,
        match="remote handoff requires an A2A control protocol",
    ):
        _finalize(
            HandoffAgent(agents=[triage, pricing]),
            _config(_CARD_URL),
            tmp_path,
        )


def test_topology_keeps_logical_name_and_hides_binding_details(tmp_path: Path) -> None:
    pricing = Agent(name="pricing", description="Produces an approved price.")
    root = SequentialAgent([Agent(name="data"), pricing], name="review")
    config = _config(_CARD_URL)
    _finalize(root, config, tmp_path)
    ctx = AgentContext(
        config=config,
        tools=[],
        card=AgentCard(name=config.name, description=""),
        agent=root,
    )

    topology = build_topology(ctx)
    nodes = {node["id"]: node for node in topology["nodes"]}
    serialized = json.dumps(topology)

    assert nodes["agent:root.step1"]["label"] == "pricing"
    assert _CARD_URL not in serialized
    assert "RemoteDatabricksAgent" not in serialized
