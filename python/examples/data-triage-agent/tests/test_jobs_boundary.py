"""Jobs capability boundary tests for the Apps user-OBO deployment."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The Apps bundle stages this repo-local package beside the agent sources.
sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent / "databricks-tools-core"))

from tools import get_job_run_history, get_job_run_logs, get_job_source_paths


@pytest.mark.parametrize(
    ("tool", "identifier", "value", "empty_key"),
    [
        (get_job_run_history, "job_id", 42, "recent_runs"),
        (get_job_run_logs, "run_id", 99, "logs"),
        (get_job_source_paths, "job_id", 42, "tasks"),
    ],
)
def test_apps_obo_jobs_scope_failure_is_typed_unavailable(
    tool, identifier, value, empty_key
) -> None:
    ws = MagicMock()
    operation = {
        get_job_run_history: ws.jobs.list_runs,
        get_job_run_logs: ws.jobs.get_run_output,
        get_job_source_paths: ws.jobs.get,
    }[tool]
    operation.side_effect = Exception(
        "Provided OAuth token does not have required scopes: jobs"
    )

    result = tool(**{identifier: value, "ws": ws})

    assert result["availability"] == "unavailable"
    assert result["capability"] == "jobs"
    assert "arbitrary dynamic Job access" in result["reason"]
    assert result[empty_key] == ([] if empty_key != "logs" else "")
    assert "OAuth token" not in str(result)


def test_unexpected_jobs_failure_still_fails_loudly() -> None:
    ws = MagicMock()
    ws.jobs.list_runs.side_effect = RuntimeError("unexpected SDK failure")

    with pytest.raises(RuntimeError, match="unexpected SDK failure"):
        get_job_run_history(job_id=42, ws=ws)
