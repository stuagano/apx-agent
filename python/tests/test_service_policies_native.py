"""Credential-free tests for the verified native Service Policy boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FIXTURE = Path(__file__).parent / "fixtures" / "service_policy_capabilities.json"
EXPECTED_TARGETS = {"mcp_service", "model_service", "model_provider_service"}
EXPECTED_KINDS = {"builtin", "llm_judge", "sql"}
EXPECTED_PHASES = {"on_call", "on_result"}


def _capabilities() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_capability_fixture_is_workspace_independent() -> None:
    capabilities = _capabilities()

    assert capabilities["schema_version"] == 1
    assert capabilities["native_apply"] is False
    assert capabilities["native_verify"] is False
    assert capabilities["abac_attachment"] is False
    assert set(capabilities["targets"]) == EXPECTED_TARGETS


def test_capability_fixture_describes_every_beta_target() -> None:
    capabilities = _capabilities()

    for target in capabilities["targets"].values():
        assert set(target["kinds"]) == EXPECTED_KINDS
        assert set(target["phases"]) == EXPECTED_PHASES
        assert set(target["ask_phases"]).issubset(set(target["phases"]))


def test_capability_fixture_contains_no_credentials_or_payloads() -> None:
    serialized = FIXTURE.read_text()

    assert "token" not in serialized.lower()
    assert "password" not in serialized.lower()
    assert "prompt" not in serialized.lower()
    assert "sql_body" not in serialized.lower()
