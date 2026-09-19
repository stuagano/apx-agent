"""Claim-vs-reality for scripts/pr-pytest-paths.sh — the PR pytest selector.

CI on pull_request must not silently fall back to `-m unit` (too thin) or
drop compile / A2A / isolation coverage. Non-PR events must still get the
full suite so GITHUB_TOKEN auto-merge can dispatch it after merge.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pr-pytest-paths.sh"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
POST_MERGE_YML = REPO / ".github" / "workflows" / "post-merge-ci.yml"


def _select(*changed: str, event: str = "pull_request") -> list[str]:
    env = os.environ.copy()
    env["EVENT_NAME"] = event
    env["PR_PYTEST_CHANGED_FILES"] = "\n".join(changed)
    # Ignore ambient CI SHAs so the fixture list is the only input.
    env.pop("BASE", None)
    env.pop("HEAD", None)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO / "python",
    )
    return proc.stdout.split()


def test_non_pr_event_is_full_suite() -> None:
    assert _select("python/src/apx_agent/cli.py", event="push") == ["tests/"]
    assert _select(event="workflow_dispatch") == ["tests/"]
    assert _select(event="") == ["tests/"]


def test_compile_change_keeps_hang_class_and_drops_cli() -> None:
    paths = _select("python/src/apx_agent/_compile.py")
    assert "tests/" not in paths
    assert "tests/test_compile.py" in paths
    assert "tests/test_parallel_context_isolation.py" in paths
    assert "tests/test_mlflow_tracing.py" in paths
    assert "tests/test_multi_hop_identity_reality_ctk.py" in paths
    assert "tests/test_declared_a2a_binding_reality_ctk.py" in paths
    assert "tests/test_cli.py" not in paths
    assert "tests/test_deploy_apps.py" not in paths


def test_cli_source_or_test_change_still_runs_cli() -> None:
    from_src = _select("python/src/apx_agent/cli.py")
    from_test = _select("python/tests/test_cli.py")
    for paths in (from_src, from_test):
        assert "tests/test_cli.py" in paths
        assert "tests/test_compile.py" in paths
        assert "tests/" not in paths


def test_wide_blast_and_unmapped_src_are_full_suite() -> None:
    assert _select("python/tests/conftest.py") == ["tests/"]
    assert _select("python/pyproject.toml") == ["tests/"]
    assert _select("python/src/apx_agent/__init__.py") == ["tests/"]
    # No test_no_such_module.py → fail-safe full, not a silent core-only run.
    assert _select("python/src/apx_agent/_no_such_module.py") == ["tests/"]


def test_workflow_only_pr_is_core_not_full() -> None:
    paths = _select(
        ".github/workflows/ci.yml",
        ".github/workflows/post-merge-ci.yml",
        "scripts/pr-pytest-paths.sh",
        "python/tests/test_pr_pytest_paths.py",
    )
    assert "tests/" not in paths
    assert "tests/test_pr_pytest_paths.py" in paths
    assert "tests/test_compile.py" in paths
    assert "tests/test_cli.py" not in paths
    all_tests = list((REPO / "python" / "tests").rglob("test_*.py"))
    assert len(paths) < len(all_tests) // 2


def test_ci_yml_dispatches_full_suite_and_uses_selector() -> None:
    body = CI_YML.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in body
    assert "scripts/pr-pytest-paths.sh" in body
    assert "pytest tests/" not in body.split("name: Run tests", 1)[1]
    assert "name: Select pytest paths" in body
    post = POST_MERGE_YML.read_text(encoding="utf-8")
    assert "schedule:" in post
    assert "gh workflow run CI" in post
    assert "secrets.GITHUB_TOKEN" in post
    assert "secrets.MY_TOKEN" not in post
    assert "personal access token" not in post.lower()
