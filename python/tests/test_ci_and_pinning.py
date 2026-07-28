"""Tests for CI template rendering and framework pin comparison."""

from __future__ import annotations

from pathlib import Path

import pytest

from apx_agent._ci_templates import (
    render_ci_files,
    render_github_workflows,
    render_gitlab_ci,
)
from apx_agent._meta import (
    PinComparison,
    compare_pinned_sha,
    read_pinned_framework_ref,
)


def test_render_github_workflows_has_three_files() -> None:
    files = render_github_workflows("demo_agent")
    assert set(files) == {
        "pr-to-main.yml",
        "pr-to-release.yml",
        "release-deploy-prod.yml",
    }
    assert "apx deploy --target apps --bundle-target staging" in files["pr-to-release.yml"]
    assert "apx deploy --target apps --bundle-target prod" in files["release-deploy-prod.yml"]
    assert "environment:" in files["release-deploy-prod.yml"]
    # GitHub expressions must survive (no accidental .format() collision).
    assert "${{ github.workflow }}" in files["pr-to-main.yml"]
    assert "${{ secrets.DATABRICKS_HOST_STAGING }}" in files["pr-to-release.yml"]


def test_render_ci_files_github_paths() -> None:
    files = render_ci_files("demo_agent", "github")
    assert ".github/workflows/pr-to-main.yml" in files
    assert ".github/workflows/release-deploy-prod.yml" in files


def test_render_gitlab_ci_substitutes_app_name() -> None:
    body = render_gitlab_ci("demo_agent")
    assert "demo_agent" in body
    assert "__APP_NAME__" not in body
    assert "bundle-target staging" in body
    assert "when: manual" in body


def test_read_pinned_framework_ref_git_url(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """\
[project]
name = "demo"
dependencies = [
  "apx-agent[langgraph] @ git+https://github.com/stuagano/apx-agent.git@abc1234#subdirectory=python",
  "mlflow>=3.0",
]
""",
        encoding="utf-8",
    )
    assert read_pinned_framework_ref(pyproject) == "abc1234"


def test_read_pinned_framework_ref_absent(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """\
[project]
name = "demo"
dependencies = ["mlflow>=3.0"]
""",
        encoding="utf-8",
    )
    assert read_pinned_framework_ref(pyproject) is None


def test_compare_pinned_sha_skips_without_git_pin(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """\
[project]
name = "demo"
dependencies = ["apx-agent[langgraph]"]
""",
        encoding="utf-8",
    )
    result = compare_pinned_sha(tmp_path)
    assert isinstance(result, PinComparison)
    assert result.skipped is True
    assert result.matches is True


def test_compare_pinned_sha_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """\
[project]
name = "demo"
dependencies = [
  "apx-agent[langgraph] @ git+https://github.com/stuagano/apx-agent.git@aaaa1111#subdirectory=python",
]
""",
        encoding="utf-8",
    )
    from apx_agent import _meta

    monkeypatch.setattr(
        _meta,
        "discover_framework_sha",
        lambda: _meta.DiscoveryResult(
            sha="bbbb2222cccccccc",
            requested_ref="bbbb2222cccccccc",
            reason="",
        ),
    )
    result = compare_pinned_sha(tmp_path)
    assert result.skipped is False
    assert result.matches is False
    assert "aaaa1111" in result.message
    assert "bbbb2222" in result.message
