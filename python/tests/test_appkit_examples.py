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
REPO_ROOT = PYTHON_ROOT.parent
EXAMPLES_ROOT = PYTHON_ROOT / "examples"
EXAMPLE_DIRS = tuple(
    sorted(
        path
        for path in EXAMPLES_ROOT.iterdir()
        if (path / "agent.py").exists() and (path / "databricks.yml").exists()
    )
)


PROBE = textwrap.dedent(
    """
    from __future__ import annotations

    import json
    import os
    from pathlib import Path

    import yaml

    from apx_agent.cli import _stage_internal_appkit_host

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
    manifest = json.loads((host_dir / "apx-host-manifest.json").read_text())
    package = json.loads((host_dir / "package.json").read_text())
    start = (host_dir / "scripts" / "start.mjs").read_text()

    assert package["dependencies"]["apx-internal-runtime"] == "file:../apx_internal_runtime"
    assert "zod" in package["dependencies"]
    assert "zod-to-json-schema" in package["dependencies"]
    assert "--preserve-symlinks" in start
    assert manifest["agent"]["name"]

    print(
        "@@APX_EXAMPLE_APPKIT@@"
        + json.dumps(
            {
                "agent": manifest["agent"]["name"],
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


@pytest.mark.parametrize("example_dir", EXAMPLE_DIRS, ids=lambda path: path.name)
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
    assert payload["logs"] == [
        "  staged internal AppKit host: .build/apx_appkit_host"
    ]
