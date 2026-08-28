from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


PYTHON_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = PYTHON_ROOT / "examples"
EXAMPLE_DIRS = tuple(
    sorted(
        path
        for path in (agent_file.parent for agent_file in EXAMPLES_ROOT.rglob("agent.py"))
        if (path / "agent.py").exists() and (path / "databricks.yml").exists()
    )
)
BRIDGE_TOOL_CASES = {
    Path("customer_triage"): {
        "name": "get_recent_orders",
        "args": {"customer_id": "cust"},
        "result_contains": "ord-cust-001",
    },
    Path("customer_triage_fleet/account_specialist"): {
        "name": "recall",
        "args": {"query": "email", "k": 1},
        "headers": {"X-Forwarded-User": "user:alice"},
        "result_contains": "email",
    },
    Path("customer_triage_fleet/billing_specialist"): {
        "name": "get_recent_orders",
        "args": {"customer_id": "cust"},
        "result_contains": "ord-cust-001",
    },
    Path("customer_triage_fleet/orchestrator"): {
        "name": "classify_intent",
        "args": {"query": "why is my invoice higher"},
        "result_contains": "billing",
    },
    Path("customer_triage_fleet/technical_specialist"): {
        "name": "docs_search",
        "args": {"query": "error"},
        "result_contains": "Troubleshooting common errors",
    },
    Path("data-triage-agent"): {
        "name": "read_github_file",
        "args": {"repo": "org/repo", "path": "pipeline.sql"},
        "result_contains": "GitHub not yet configured",
    },
    Path("eligibility-agent"): {
        "name": "compute_income",
        "args": {
            "parsed": {
                "documents": [
                    {
                        "document_type": "w2",
                        "extracted": {
                            "employee_name": "Ada Lovelace",
                            "annual_wages": 52000,
                        },
                    }
                ]
            }
        },
        "result_contains": "52000",
    },
    Path("entity-resolution-agent"): {
        "name": "normalize_record",
        "args": {"name": "  ada lovelace  ", "address": "  1 main st  "},
        "result_contains": "Ada Lovelace",
    },
    Path("explain-my-bill-agent"): {
        "name": "get_session_context",
        "args": {},
        "headers": {"X-Forwarded-Email": "alice@example.com"},
        "result_contains": "alice@example.com",
    },
    Path("memory_demo"): {
        "name": "recall",
        "args": {"query": "window seat", "k": 2},
        "headers": {"X-Forwarded-User": "alice"},
        "result_contains": "window seats",
    },
    Path("shortage-intelligence-agent"): {
        "name": "classify_shortage_severity",
        "args": {
            "avg_price_delta_pct": 30.0,
            "max_price_delta_pct": 35.0,
            "customer_count": 2,
            "similar_events_found": 3,
        },
        "result_contains": "HIGH",
    },
}


PROBE = textwrap.dedent(
    """
    from __future__ import annotations

    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apx_agent import AgentConfig
    import apx_agent._appkit_tool_bridge as bridge_module
    from apx_agent._appkit_tool_bridge import build_appkit_tool_bridge_router
    from apx_agent._models import AgentCard, AgentContext
    from apx_agent.cli import _load_finalized_agent, _stage_internal_appkit_host

    root = Path.cwd()
    doc = yaml.safe_load((root / "databricks.yml").read_text()) or {}
    apps = (doc.get("resources") or {}).get("apps") or {}
    if not apps:
        raise AssertionError("databricks.yml does not declare an app resource")
    bundle_key = next(iter(apps))
    app = apps[bundle_key]
    config = app.setdefault("config", {})
    env = config.setdefault("env", [])
    for item in env:
        if isinstance(item, dict) and item.get("name") == "APX_APPS_HOST":
            item["value"] = "appkit"
            break
    else:
        env.append({"name": "APX_APPS_HOST", "value": "appkit"})

    (root / ".build").mkdir(exist_ok=True)
    logs = []
    _stage_internal_appkit_host(
        root,
        module="agent:agent",
        doc=doc,
        bundle_key=bundle_key,
        log=logs.append,
    )

    host_dir = root / ".build" / "apx_appkit_host"
    bridge_dir = root / ".build" / "agent_server"
    manifest = json.loads((host_dir / "apx-host-manifest.json").read_text())
    package = json.loads((host_dir / "package.json").read_text())
    start = (host_dir / "scripts" / "start.mjs").read_text()

    assert (root / ".build" / "agent.py").exists()
    assert (bridge_dir / "__init__.py").exists()
    assert (bridge_dir / "start_server.py").exists()
    if (root / "agent.config.yaml").exists():
        assert (root / ".build" / "agent.config.yaml").exists()
    assert package["dependencies"]["apx-internal-runtime"] == "file:../apx_internal_runtime"
    assert "zod" in package["dependencies"]
    assert "zod-to-json-schema" in package["dependencies"]
    assert "--preserve-symlinks" in start
    assert manifest["agent"]["name"]

    agent = _load_finalized_agent("agent:agent")
    app = FastAPI()
    app.state.agent_context = AgentContext(
        config=AgentConfig(
            name=manifest["agent"]["name"],
            model=manifest["agent"]["model"],
        ),
        tools=[],
        card=AgentCard(
            name=manifest["agent"]["name"],
            description=(
                manifest["agent"]["description"]
                if "description" in manifest["agent"]
                else ""
            ),
        ),
        agent=agent,
    )
    bridge_module._obo_ws_from_headers = lambda _: None
    app.include_router(build_appkit_tool_bridge_router())
    client = TestClient(app)
    response = client.post(
        "/_apx/internal/appkit/tools/__missing__",
        json={"args": {}},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown APX tool: __missing__"

    bridge_env = dict(os.environ)
    bridge_pythonpath_parts = [str(root / ".build")]
    if bridge_env.get("PYTHONPATH"):
        bridge_pythonpath_parts.append(bridge_env["PYTHONPATH"])
    bridge_env["PYTHONPATH"] = os.pathsep.join(bridge_pythonpath_parts)
    bridge_boot = subprocess.run(
        [
            sys.executable,
            "-c",
            "from apx_agent.cli import _load_finalized_agent; _load_finalized_agent('agent:agent')",
        ],
        cwd=root / ".build",
        env=bridge_env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert bridge_boot.returncode == 0, bridge_boot.stderr + bridge_boot.stdout

    executed_tool = None
    tool_case_raw = os.environ.get("APX_APPKIT_TOOL_CASE")
    if tool_case_raw is not None:
        tool_case = json.loads(tool_case_raw)
        headers = {
            "X-Forwarded-User": "alice",
            "X-Forwarded-Email": "alice@example.com",
            **tool_case.get("headers", {}),
        }
        tool_response = client.post(
            f"/_apx/internal/appkit/tools/{tool_case['name']}",
            json={"args": tool_case["args"]},
            headers=headers,
        )
        assert tool_response.status_code == 200, tool_response.text
        result_text = json.dumps(tool_response.json()["result"], sort_keys=True)
        expected = tool_case["result_contains"]
        assert expected in result_text, result_text
        executed_tool = tool_case["name"]

    print(
        "@@APX_EXAMPLE_APPKIT@@"
        + json.dumps(
            {
                "agent": manifest["agent"]["name"],
                "executed_tool": executed_tool,
                "logs": logs,
                "tools": [tool["name"] for tool in manifest.get("tools", [])],
            },
            sort_keys=True,
        )
    )
    """
).strip()


def _copy_example(src: Path, dst: Path) -> None:
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(
            ".build",
            ".mypy_cache",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "dist",
            "node_modules",
        ),
    )


@pytest.mark.parametrize(
    "example_dir",
    EXAMPLE_DIRS,
    ids=lambda path: str(path.relative_to(EXAMPLES_ROOT)),
)
def test_example_agent_stages_internal_appkit_host(
    tmp_path: Path, example_dir: Path
) -> None:
    workdir = tmp_path / example_dir.name
    _copy_example(example_dir, workdir)
    pythonpath_parts = [
        str(workdir),
        str(PYTHON_ROOT / "src"),
        str(EXAMPLES_ROOT / "databricks-tools-core"),
    ]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    if existing_pythonpath is not None:
        pythonpath_parts.append(existing_pythonpath)
    rel = example_dir.relative_to(EXAMPLES_ROOT)

    env = {
        **os.environ,
        "APX_AGENT_MLFLOW_AUTOLOG": "0",
        "APX_SMOKE_MODE": "1",
        "DEMO_MODE": "true",
        "DATABRICKS_CONFIG_FILE": os.devnull,
        "DATABRICKS_HOST": "http://127.0.0.1:9",
        "DATABRICKS_TOKEN": "local-test-token",
        "DATABRICKS_WORKSPACE_ID": "local-test-workspace",
        "MLFLOW_TRACKING_URI": f"file:{tmp_path / 'mlruns'}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(pythonpath_parts),
    }
    tool_case = BRIDGE_TOOL_CASES.get(rel)
    if tool_case is not None:
        env["APX_APPKIT_TOOL_CASE"] = json.dumps(tool_case)
    env.pop("DATABRICKS_CONFIG_PROFILE", None)

    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=workdir,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    marker = next(
        line
        for line in result.stdout.splitlines()
        if line.startswith("@@APX_EXAMPLE_APPKIT@@")
    )
    payload = json.loads(marker.removeprefix("@@APX_EXAMPLE_APPKIT@@"))
    assert payload["agent"]
    if tool_case is not None:
        assert payload["executed_tool"] == tool_case["name"]
    assert payload["logs"] == [
        "  staged internal AppKit host: .build/apx_appkit_host"
    ]
