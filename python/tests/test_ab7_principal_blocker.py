"""The apx-ab7 record is a principal blocker, not a live approval pass."""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RECORD = _ROOT / "docs/evidence/apx-ab7-2026-11-21/evidence.json"
_PAGE = _ROOT / "docs/design/durable-host-consumers.md"


def test_committed_principal_survey_does_not_claim_a_live_approval() -> None:
    record = json.loads(_RECORD.read_text())
    page = _PAGE.read_text()
    assert record["result"] == "blocked"
    assert record["live_pass"] is False
    assert record["fault_injection"] is False
    assert record["http_invocations_sent"] == 0
    assert record["approval_created"] is False
    assert record["apps_stop_or_start"] is False
    assert record["deployment_changed"] is False
    assert record["second_principal_created"] is False
    assert record["pat_minted"] is False
    assert record["app_service_principal_used_as_caller"] is False
    assert record["other_workspace_profiles_used"] is False
    assert record["app"]["name"] == "agent-bricks-apx-dur-gen"
    assert record["app"]["app_status_state"] == "RUNNING"
    assert record["app"]["running_instances"] == 1
    assert record["app"]["deployment_id"] == "01f1e546718010678017040e46a17107"
    workspace = record["profiles"]["workspace_7474660401027952"]
    assert [item["name"] for item in workspace] == ["fevm-stable"]
    assert workspace[0]["auth_type"] == "databricks-cli"
    assert record["profiles"]["other_workspace_profile_count"] == 4
    check = record["profiles"]["sdk_check"]
    assert check["auth_type"] == "databricks-cli"
    assert check["auth_type_is_pat"] is False
    assert check["personal_access_token_prefix"] is False
    assert check["token_printed"] is False
    symbols = record["symbols_inspected"]
    assert symbols["sdk_symbol"] == "DurableAgentServer._execute"
    assert symbols["recovery_error_code"] == "MCP_USER_AUTH_RECOVERY_UNSUPPORTED"
    assert symbols["apx_manifest_symbol"] == "build_native_manifest"
    assert symbols["approval_interrupt_calls"] == "langgraph.types.interrupt"
    assert symbols["approval_store_durable"] is False
    assert record["acceptance"]["unauthorized_continuation_denied"] is False
    assert record["acceptance"]["authorized_continuation_once"] is False
    assert record["acceptance"]["duplicate_resume_blocked"] is False
    assert record["left_running"] is True
    blob = _RECORD.read_text() + (_ROOT / "docs/evidence/apx-ab7-2026-11-21/README.md").read_text()
    for needle in ("Bearer ", "stuart.gano@", "74813804165893", "pypi-proxy"):
        assert needle not in blob
    assert "../evidence/apx-ab7-2026-11-21/README.md" in page
    assert "MCP_USER_AUTH_RECOVERY_UNSUPPORTED" in page
