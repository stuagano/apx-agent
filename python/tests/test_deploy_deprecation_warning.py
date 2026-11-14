"""Deploy-time ``responses_agent`` deprecation warning (#1143).

#1134 warns about the deprecated ``responses_agent`` target in two places. The
compile-time call is covered by ``test_only_responses_target_warns_deprecated``
in ``test_runtime_targets.py``; these tests guard the deploy-time
``warn_if_deprecated_target`` call in ``_deploy_apps_impl`` (right after
``_config_from_project_or_agent``), which nothing failed while it was untested.
"""

from __future__ import annotations

import sys
import types
import warnings
from pathlib import Path

import pytest
from click.testing import CliRunner

from apx_agent.cli import main
from tests.test_deploy_apps import _install_subprocess_mock
from tests.test_deploy_apps import scaffold as scaffold  # noqa: F401 — re-exported fixture


@pytest.fixture(autouse=True)
def _stub_compile_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ``apx_agent._responses_agent`` exists so validation passes."""
    stub = types.ModuleType("apx_agent._responses_agent")
    stub.compile_to_responses_agent = lambda *_a, **_k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apx_agent._responses_agent", stub)


@pytest.mark.parametrize("target", ["responses_agent", "durable_agent_server"])
def test_deploy_apps_warns_only_for_deprecated_responses_target(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    """The deploy-time warning fires once for a ``responses_agent`` project
    (naming ``durable_agent_server``) and never for a durable one."""
    (scaffold / "pyproject.toml").write_text(
        '[project]\nname = "my-app"\n\n[tool.apx.deploy]\nname = "my-app"\n'
    )
    (scaffold / "agent.py").write_text(
        "from apx_agent import LlmAgent\n"
        f"agent = LlmAgent(name='my-app', model='declared-model', target='{target}')\n"
    )
    _install_subprocess_mock(monkeypatch)
    args = ["agents", "deploy", "--target", "apps", "--bundle-target", "dev"]
    if target == "durable_agent_server":
        # A durable project on an existing Bundle deploys through Agent Bricks,
        # which requires an explicit profile.
        args.extend(["--profile", "chosen"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = CliRunner().invoke(main, args)
    deprecations = [
        w for w in caught
        if issubclass(w.category, FutureWarning) and "responses_agent" in str(w.message)
    ]
    if target == "responses_agent":
        assert result.exit_code == 0, result.output
        assert len(deprecations) == 1
        assert "durable_agent_server" in str(deprecations[0].message)
    else:
        # The Agent Bricks rollout past the warning site needs a real workspace;
        # this leg only guards that the deploy read the durable config (it got
        # past the warning site) without emitting the deprecation warning.
        assert "# resolved app_name: my-app" in result.output, result.output
        assert deprecations == []
