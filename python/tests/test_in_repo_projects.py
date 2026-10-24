"""In-repo projects declare settings in Python, not [tool.apx.agent] (sub-project 3c)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from apx_agent._inspection import _load_agent_config

_REPO = Path(__file__).resolve().parents[2]
_MAIN = json.loads((Path(__file__).parent / "fixtures" / "in_repo_projects_main.json").read_text())
_MIGRATED: dict[str, str] = {  # project dir -> its deploy name
    "python/examples/bakehouse-agent": "bakehouse-agent",
    "python/examples/customer_triage": "customer_triage",
    "python/examples/customer_triage_fleet/account_specialist": "account_specialist",
    "python/examples/customer_triage_fleet/billing_specialist": "billing_specialist",
    "python/examples/customer_triage_fleet/orchestrator": "customer_triage",
    "python/examples/customer_triage_fleet/technical_specialist": "technical_specialist",
    "python/examples/eligibility-agent": "eligibility-agent",
    "python/examples/explain-my-bill-agent": "explain_my_bill_agent",
    "python/examples/hubspot-complaints-agent": "hubspot-complaints-agent",
    "python/examples/memory_demo": "memory_demo",
    "python/examples/pe55-support-agent": "pe55_support_agent",
    "python/examples/plg-discovery": "discovery",
    "python/examples/sec-10k-agent": "sec_10k_agent",
    "python/examples/shortage-intelligence-agent": "shortage_intelligence_agent",
    "python/examples/slack-agent": "slack-agent",
    "python/examples/zero-ops-diagnostics": "zero-ops-diagnostics",
    "python/hello-world": "hello-world",
    "python/payroll-coworker": "payroll-coworker",
    "python/examples/apx-builder": "apx_builder",
    "python/examples/contract-parsing-agent": "contract_parsing_agent",
    "python/examples/data-inspector": "data_inspector",
    "python/examples/data-triage-agent": "data_triage_agent",
    "python/examples/entity-resolution-agent": "entity_resolution_agent",
    "python/samples-accuweather": "samples-accuweather",
    "python/samples-bakehouse": "samples-bakehouse",
    "python/samples-healthverity": "samples-healthverity",
    "python/samples-hub": "samples-hub",
    "python/samples-tpcds-sf1": "samples-tpcds-sf1",
    "python/samples-tpcds-sf1000": "samples-tpcds-sf1000",
    "python/samples-tpch": "samples-tpch",
    "python/samples-wanderbricks": "samples-wanderbricks",
}

# Import and finalize one project in a fresh interpreter (its tools/ and config.py never
# shadow another project's) and print what it would serve.
_SERVED = r'''
import hashlib, json, runpy, sys
from pathlib import Path
root = Path(sys.argv[1]); sys.path.insert(0, str(root))
from apx_agent._inspection import _load_agent_config
from apx_agent._settings import resolve_settings
from apx_agent._wiring import finalize_agent
agent = runpy.run_path(str(root / "agent.py"))["agent"]
config = resolve_settings(agent, _load_agent_config(pyproject_path=root / "pyproject.toml"))
finalize_agent(agent, config, pyproject_path=str(root / "pyproject.toml"))
instructions = getattr(agent, "_instructions", None)
dump = lambda m: None if m is None else m.model_dump(mode="json")
print(json.dumps({
    "name": config.name, "model": config.model, "target": config.target,
    "description": config.description, "examples": config.examples, "knowledge": config.knowledge,
    "session": dump(config.session), "memory": dump(config.memory),
    "max_iterations": getattr(agent, "_max_iterations", None),
    "instructions_sha256": None if instructions is None else hashlib.sha256(instructions.encode()).hexdigest(),
}))
'''


def _served(root: Path) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABRICKS_")}  # no workspace
    out = subprocess.run([sys.executable, "-c", _SERVED, str(root)], cwd=root, env=env,
                         capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _tracked_projects() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "*pyproject.toml"], cwd=_REPO,
                            capture_output=True, text=True, check=True).stdout.split()
    return [str(Path(p).parent) for p in listed if "tests" not in Path(p).parts]


def test_no_project_declares_the_agent_table() -> None:
    offenders = sorted(
        d for d in _tracked_projects()
        if "agent" in tomllib.loads((_REPO / d / "pyproject.toml").read_text()).get("tool", {}).get("apx", {}))
    assert offenders == [], "run `apx-agent agents migrate-config` in: " + ", ".join(offenders)


@pytest.mark.parametrize("project", sorted(_MIGRATED))
def test_migrated_project_loads_with_its_name(project: str) -> None:
    pyproject = _REPO / project / "pyproject.toml"
    assert "agent" not in tomllib.loads(pyproject.read_text()).get("tool", {}).get("apx", {})
    config = _load_agent_config(pyproject_path=pyproject)
    assert config is not None and config.name == _MIGRATED[project]


@pytest.mark.parametrize("project", sorted(_MAIN))
def test_project_serves_what_main_served(project: str) -> None:
    served = _served(_REPO / project)
    assert {k: served[k] for k in _MAIN[project]} == _MAIN[project]  # memory_demo's prompt isn't deterministic


def test_apx_builder_settings_reach_the_cli() -> None:
    # apx-builder serves app.py; the CLI reads settings through agent.py (agent:agent).
    root = _REPO / "python" / "examples" / "apx-builder"
    script = ("import sys; sys.path.insert(0, '.'); from pathlib import Path; from apx_agent.cli import _effective_config; "
              "c = _effective_config(Path('.')); print(c.model, '|', c.description)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABRICKS_")}
    out = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == (
        "databricks-claude-sonnet-4-6 | Natural language agent builder — describe your agent and I'll build and deploy it")


def test_payroll_coworker_keeps_history_without_a_declared_session() -> None:
    # Its Delta session was dropped (3c); like samples-*, fall back to an in-process store.
    server = (_REPO / "python" / "payroll-coworker" / "agent_server" / "start_server.py").read_text()
    assert "if _conversation_store is None:" in server and "InMemoryConversationStore()" in server
