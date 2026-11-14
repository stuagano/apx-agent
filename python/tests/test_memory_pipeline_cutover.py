"""#858 / ADR 0001: apx's own extraction is gone; a memory pipeline replaces it."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from apx_agent import AgentConfig, MemoryBackendConfig
from apx_agent._doctor import Status, check_memory_pipeline


@pytest.mark.parametrize("key, value", [
    ("extraction", {"enabled": True}),
    ("strategies", ["facts"]),
    ("strategy_prompts", {"facts": "x"}),
    ("extract_every_n_turns", 3),
])
def test_removed_extraction_keys_fail_with_the_pipeline_command(key: str, value: Any) -> None:
    with pytest.raises(ValidationError) as exc:
        MemoryBackendConfig.model_validate({"type": "managed", key: value})
    message = str(exc.value)
    assert f"memory.{key} was removed" in message
    assert "agentbricks memory pipeline create" in message


def _managed() -> AgentConfig:
    return AgentConfig(name="my-agent", model="m", memory={"type": "managed"}, session={"type": "managed"})


def _check(tmp_path: Path, cfg: AgentConfig, pages: list[dict[str, Any]] | None, *, auth_ok: bool = True) -> Any:
    ws = MagicMock()
    ws.api_client.do.side_effect = pages if pages is not None else AssertionError("no workspace call expected")
    with patch("apx_agent._doctor._is_apx_project", return_value=True), patch(
        "apx_agent._doctor_durable.effective_config_isolated", return_value=cfg
    ), patch("apx_agent._defaults._make_workspace_client", return_value=ws):
        return check_memory_pipeline(tmp_path, auth_ok=auth_ok), ws


def _pipeline(session: str, memory: str) -> dict[str, str]:
    return {"session_store": f"session-stores/{session}", "memory_store": f"memory-stores/{memory}"}


def test_doctor_ok_when_a_later_page_has_the_pair(tmp_path: Path) -> None:
    pages = [
        {"memory_pipelines": [_pipeline("other-sessions", "other-memory")], "next_page_token": "p2"},
        {"memory_pipelines": [_pipeline("apx-my-agent-sessions", "apx-my-agent-memory")]},
    ]
    check, ws = _check(tmp_path, _managed(), pages)
    assert check.status is Status.OK
    assert ws.api_client.do.call_args_list[1].kwargs["query"] == {"page_token": "p2"}


def test_doctor_warns_with_the_create_command_when_no_pipeline(tmp_path: Path) -> None:
    check, _ = _check(tmp_path, _managed(), [{"memory_pipelines": [_pipeline("apx-my-agent-sessions", "x")]}])
    assert check.status is Status.WARN
    assert check.fix.startswith(
        "agentbricks memory pipeline create --session-store apx-my-agent-sessions "
        "--memory-store apx-my-agent-memory"
    )


def test_doctor_offline_skips_without_a_workspace_call(tmp_path: Path) -> None:
    check, ws = _check(tmp_path, _managed(), None, auth_ok=False)
    assert check.status is Status.SKIP
    ws.api_client.do.assert_not_called()


def test_doctor_silent_unless_both_stores_are_managed(tmp_path: Path) -> None:
    cfg = AgentConfig(name="my-agent", model="m", memory={"type": "inmemory"}, session={"type": "managed"})
    check, _ = _check(tmp_path, cfg, None)
    assert check is None
