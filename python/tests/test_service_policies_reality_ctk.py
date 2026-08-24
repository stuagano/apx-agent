"""Claim-versus-reality checks for live hooks and native plan artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apx_agent import AgentConfig, LlmAgent
from apx_agent._service_policies_native import build_native_policy_plan
from apx_agent._wiring import apply_config_service_policies
from ctk import Artifact, verify


def _config() -> AgentConfig:
    return AgentConfig.model_validate({
        "name": "reality-agent",
        "service_policies": {
            "native_mode": "plan",
            "attachments": [{
                "name": "reality-judge",
                "target_type": "mcp_service",
                "target": "main.tools.github",
                "policies": [{
                    "name": "external-write-review",
                    "kind": "llm_judge",
                    "classifier": "judge-model",
                    "prompt": "private classifier rubric that must not escape the plan",
                    "phase": "on_call",
                    "rank": 100,
                }],
            }],
        },
    })


@pytest.mark.unit
def test_real_leaf_hook_and_native_plan_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    agent = LlmAgent(name="leaf")
    apply_config_service_policies(agent, config)

    plan = build_native_policy_plan(config.service_policies)
    artifact = tmp_path / "service-policy-plan.json"
    artifact.write_text(json.dumps({
        "native_mode": plan.native_mode.value,
        "operations": plan.operations,
        "unsupported": plan.unsupported,
        "declaration_fingerprint": plan.declaration_fingerprint,
    }, indent=2))

    verify(Artifact(
        str(artifact),
        min_bytes=100,
        must_contain="external-write-review",
    ))
    assert "private classifier rubric" not in artifact.read_text()
    assert agent.__dict__.get("_apx_service_policies_applied", False) is True
    assert agent._before_tool is not None

    # Replace the local judge with an injected deny through the adapter that
    # reached the leaf; this invokes the live hook, not a wrapper-only helper.
    adapter = agent.__dict__["_apx_service_policy_adapter"]
    adapter._evaluators["external-write-review"] = lambda event: "DENY"
    with pytest.raises(PermissionError):
        agent._before_tool("delete_repo", {"name": "demo"})
