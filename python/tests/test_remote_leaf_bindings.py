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


@pytest.mark.parametrize(
    "reference",
    [
        "https://NON_SECRET_USER:NON_SECRET_PASSWORD@pricing.example/card",
        "https://pricing.example/card?NON_SECRET_QUERY=value",
        "https://pricing.example/card#NON_SECRET_FRAGMENT",
        "https://pricing.example:NON_SECRET_PORT/card",
        "https://[NON_SECRET_BAD_IPV6]/card",
    ],
)
def test_invalid_binding_diagnostic_never_echoes_resolved_location(
    reference: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRICING_APP_URL", reference)
    root = SequentialAgent([Agent(name="pricing")])
    with pytest.raises(ValueError, match="binding 'pricing'.*malformed A2A card URL") as error:
        _finalize(root, _config(), tmp_path)
    assert "NON_SECRET" not in str(error.value)
    assert reference not in str(error.value)


def test_duplicate_logical_leaf_name_fails_closed(tmp_path: Path) -> None:
    root = SequentialAgent(
        [Agent(name="pricing"), Agent(name="pricing")],
        name="review",
    )

    with pytest.raises(ValueError, match="binding 'pricing'.*found 2"):
        _finalize(root, _config(_CARD_URL), tmp_path)


def test_remote_loop_body_binding_resolves(tmp_path: Path) -> None:
    # AC-6: the old guard is gone — a remote loop body now resolves like a
    # sequential leaf; control routes via the typed ControlSignal on the reply.
    pricing = Agent(name="pricing")
    root = LoopAgent(pricing)

    _finalize(root, _config(_CARD_URL), tmp_path)

    assert root._apx_remote_leaf_bindings["pricing"].card_url == _CARD_URL


def test_remote_handoff_peer_binding_resolves_with_sibling_allowlist(
    tmp_path: Path,
) -> None:
    # AC-6: the handoff guard is gone; the peer resolves and carries the local
    # sibling allowlist (FR-4) so a reconstructed transfer_to:<target> can be
    # validated against the local graph.
    triage = Agent(name="triage")
    pricing = Agent(name="pricing")
    root = HandoffAgent(agents=[triage, pricing])

    _finalize(root, _config(_CARD_URL), tmp_path)

    resolved = root._apx_remote_leaf_bindings["pricing"]
    assert resolved.card_url == _CARD_URL
    assert resolved.transfer_targets == frozenset({"triage"})


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
