"""Reality checks for Service Policy configuration and generated projects."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from apx_agent import AgentConfig, LlmAgent
from apx_agent._project_gen import _build_pyproject
from apx_agent._service_policies import ServicePoliciesConfig
from apx_agent._wiring import apply_config_service_policies
from apx_agent._yaml_spec import SpecValidationError, load_spec


def _config(*, mode: str = "enforce") -> AgentConfig:
    return AgentConfig.model_validate({
        "name": "policy-agent",
        "service_policies": {
            "local_mode": "mirror",
            "native_mode": "plan",
            "attachments": [{
                "name": "tool-guard",
                "target_type": "mcp_service",
                "target": "main.tools.github",
                "mode": mode,
                "policies": [{
                    "name": "block-sql",
                    "kind": "sql",
                    "function": "main.governance.check_event",
                    "phase": "on_call",
                    "rank": 10,
                }],
            }],
            "abac": {"tags": {"service_class": "customer-facing"}},
        },
    })


def test_service_policies_attach_to_leaf_agent_once() -> None:
    agent = LlmAgent(name="leaf")
    config = _config()

    apply_config_service_policies(agent, config)
    first = agent._before_tool
    apply_config_service_policies(agent, config)

    assert agent._before_tool is first
    assert agent.__dict__.get("_apx_service_policies_applied", False) is True
    with pytest.raises(PermissionError):
        agent._before_tool("delete_repo", {"name": "demo"})


def test_service_policies_use_the_shared_config_seam() -> None:
    config = _config()
    agent = LlmAgent(name="leaf")

    # finalize_agent is the shared runtime/deploy seam; direct application is
    # tested above so this proves the declaration reaches the live leaf there.
    from apx_agent._wiring import finalize_agent

    finalize_agent(agent, config)

    assert agent.__dict__.get("_apx_service_policies_applied", False) is True
    assert agent.__dict__.get("_apx_service_policy_adapter") is not None


def test_generated_pyproject_round_trips_service_policy_config() -> None:
    config = _config()
    parsed = tomllib.loads(_build_pyproject(config))
    raw = parsed["tool"]["apx"]["agent"]["service_policies"]
    round_tripped = ServicePoliciesConfig.model_validate(raw)

    assert round_tripped.model_dump(mode="json") == config.service_policies.model_dump(mode="json")


def test_yaml_service_policies_resolve_environment_references(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_NAME", "main.tools.github")
    spec = tmp_path / "agent.yaml"
    spec.write_text(
        """name: yaml-agent
service_policies:
  native_mode: plan
  attachments:
    - name: yaml-guard
      target_type: mcp_service
      target: $SERVICE_NAME
      policies:
        - name: sql-guard
          kind: sql
          function: main.governance.check_event
          phase: on_call
"""
    )

    config = load_spec(spec)

    assert config.service_policies.attachments[0].target == "main.tools.github"


def test_yaml_service_policies_reject_unresolved_references(tmp_path: Path) -> None:
    spec = tmp_path / "agent.yaml"
    spec.write_text(
        """name: yaml-agent
service_policies:
  attachments:
    - name: yaml-guard
      target_type: mcp_service
      target: $SERVICE_NAME
      policies: []
"""
    )

    with pytest.raises(SpecValidationError, match="SERVICE_NAME"):
        load_spec(spec, strict=True)
