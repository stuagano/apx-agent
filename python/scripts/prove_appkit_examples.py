#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml


REPO = Path(__file__).resolve().parents[2]
PYTHON_ROOT = REPO / "python"
EXAMPLES_ROOT = PYTHON_ROOT / "examples"
DEFAULT_PYTHON = PYTHON_ROOT / ".venv" / "bin" / "python"
FAKE_DATABRICKS_PORT = 19490
BASE_APP_PORT = 19500
BASE_BRIDGE_PORT = 19600

BRIDGE_TOOL_CASES: dict[str, dict[str, Any]] = {
    "customer_triage": {
        "name": "get_recent_orders",
        "args": {"customer_id": "cust"},
        "headers": {},
        "result_contains": "ord-cust-001",
    },
    "customer_triage_fleet/account_specialist": {
        "name": "recall",
        "args": {"query": "email", "k": 1},
        "headers": {"X-Forwarded-User": "user:alice"},
        "result_contains": "email",
        "compare_exact": False,
    },
    "customer_triage_fleet/billing_specialist": {
        "name": "get_recent_orders",
        "args": {"customer_id": "cust"},
        "headers": {},
        "result_contains": "ord-cust-001",
    },
    "customer_triage_fleet/orchestrator": {
        "name": "classify_intent",
        "args": {"query": "why is my invoice higher"},
        "headers": {},
        "result_contains": "billing",
    },
    "customer_triage_fleet/technical_specialist": {
        "name": "docs_search",
        "args": {"query": "error"},
        "headers": {},
        "result_contains": "Troubleshooting common errors",
    },
    "data-triage-agent": {
        "name": "read_github_file",
        "args": {"repo": "org/repo", "path": "pipeline.sql"},
        "headers": {},
        "result_contains": "GitHub not yet configured",
    },
    "eligibility-agent": {
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
        "headers": {},
        "result_contains": "52000",
    },
    "entity-resolution-agent": {
        "name": "normalize_record",
        "args": {"name": "  ada lovelace  ", "address": "  1 main st  "},
        "headers": {},
        "result_contains": "Ada Lovelace",
    },
    "explain-my-bill-agent": {
        "name": "get_session_context",
        "args": {},
        "headers": {"X-Forwarded-Email": "alice@example.com"},
        "result_contains": "alice@example.com",
    },
    "memory_demo": {
        "name": "recall",
        "args": {"query": "window seat", "k": 2},
        "headers": {"X-Forwarded-User": "alice"},
        "result_contains": "alice",
        "compare_exact": False,
    },
    "shortage-intelligence-agent": {
        "name": "classify_shortage_severity",
        "args": {
            "avg_price_delta_pct": 30.0,
            "max_price_delta_pct": 35.0,
            "customer_count": 2,
            "similar_events_found": 3,
        },
        "headers": {},
        "result_contains": "HIGH",
    },
}


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: str


class FakeDatabricks(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _write(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.endswith("/api/2.0/preview/scim/v2/Me"):
            self._write({"userName": "alice@example.com", "id": "alice"})
            return
        if self.path.endswith("/.well-known/databricks-config"):
            self._write({"workspace_id": "local-proof-workspace"})
            return
        self._write({})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", "0") or "0"))
        if self.path.endswith("/invocations"):
            self._write(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "local fake model response",
                            }
                        }
                    ]
                }
            )
            return
        self._write({})


class ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


@contextlib.contextmanager
def fake_databricks() -> Any:
    server = ReuseTCPServer(("127.0.0.1", FAKE_DATABRICKS_PORT), FakeDatabricks)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


def discover_examples() -> list[Path]:
    examples = []
    for agent_file in EXAMPLES_ROOT.rglob("agent.py"):
        example = agent_file.parent
        if (example / "databricks.yml").exists():
            examples.append(example.relative_to(EXAMPLES_ROOT))
    return sorted(examples)


def env(
    tmp: Path,
    build_dir: Path,
    python: Path,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ.copy()
    values.pop("DATABRICKS_CONFIG_PROFILE", None)
    values.update(
        {
            "APX_AGENT_MLFLOW_AUTOLOG": "0",
            "APX_APPS_HOST": "appkit",
            "APX_SMOKE_MODE": "1",
            "DATABRICKS_CONFIG_FILE": os.devnull,
            "DATABRICKS_HOST": f"http://127.0.0.1:{FAKE_DATABRICKS_PORT}",
            "DATABRICKS_TOKEN": "local-proof-token",
            "DATABRICKS_WORKSPACE_ID": "local-proof-workspace",
            "DEMO_MODE": "true",
            "MLFLOW_TRACKING_URI": f"file:{tmp / 'mlruns'}",
            "PYTHON": str(python),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(build_dir),
                    str(PYTHON_ROOT / "src"),
                    str(EXAMPLES_ROOT / "databricks-tools-core"),
                ]
            ),
        }
    )
    if extra:
        values.update(extra)
    return values


def copy_example(src: Path, dst: Path) -> None:
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


def enable_appkit(workdir: Path) -> None:
    doc = yaml.safe_load((workdir / "databricks.yml").read_text()) or {}
    apps = (doc.get("resources") or {}).get("apps") or {}
    if not apps:
        raise RuntimeError(f"{workdir} has no Databricks app resource")
    bundle_key = next(iter(apps))
    config = apps[bundle_key].setdefault("config", {})
    env_list = config.setdefault("env", [])
    for item in env_list:
        if isinstance(item, dict) and item.get("name") == "APX_APPS_HOST":
            item["value"] = "appkit"
            break
    else:
        env_list.append({"name": "APX_APPS_HOST", "value": "appkit"})
    (workdir / "databricks.yml").write_text(yaml.safe_dump(doc, sort_keys=False))


def stage_example(workdir: Path, python: Path, tmp: Path) -> None:
    build_dir = workdir / ".build"
    build_dir.mkdir(exist_ok=True)
    code = """
from pathlib import Path
import yaml
from apx_agent.cli import _stage_build_manifest, _stage_internal_appkit_host
root = Path.cwd()
doc = yaml.safe_load((root / 'databricks.yml').read_text()) or {}
bundle_key = next(iter(((doc.get('resources') or {}).get('apps') or {})))
_stage_internal_appkit_host(
    root,
    module='agent:agent',
    doc=doc,
    bundle_key=bundle_key,
    log=lambda message: print(message),
)
_stage_build_manifest(root / '.build', None)
"""
    subprocess.run(
        [str(python), "-c", code],
        cwd=workdir,
        env=env(tmp, build_dir, python),
        check=True,
        text=True,
    )


def wait_url(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return
        except Exception as exc:
            last = exc
            time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


def wait_tcp(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last: OSError | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError as exc:
            last = exc
            time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for 127.0.0.1:{port}: {last}")


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> HttpResult:
    request_headers = {
        "content-type": "application/json",
        "x-forwarded-user": "alice",
        "x-forwarded-email": "alice@example.com",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return HttpResult(status=response.status, body=response.read().decode())
    except urllib.error.HTTPError as exc:
        return HttpResult(status=exc.code, body=exc.read().decode())


def terminate(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=8)
    if proc.poll() is None:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def install_once(host_dir: Path, install_source: Path | None, run_env: dict[str, str]) -> Path:
    if install_source is None:
        subprocess.run(["npm", "install"], cwd=host_dir, env=run_env, check=True, timeout=180)
        return host_dir / "node_modules"
    os.symlink(install_source, host_dir / "node_modules", target_is_directory=True)
    return install_source


def verify_direct_tool(
    build_dir: Path,
    tmp: Path,
    python: Path,
    tool_case: dict[str, Any],
    direct_port: int,
) -> HttpResult:
    proc = subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "agent_server.start_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(direct_port),
        ],
        cwd=build_dir,
        env=env(tmp, build_dir, python, {"APX_PYTHON_BRIDGE_CWD": str(build_dir)}),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_tcp(direct_port)
        return post_json(
            f"http://127.0.0.1:{direct_port}/_apx/internal/appkit/tools/{tool_case['name']}",
            {"args": tool_case["args"]},
            tool_case["headers"],
        )
    finally:
        terminate(proc)


def verify_example(
    rel: Path,
    tmp: Path,
    python: Path,
    install_source: Path | None,
    index: int,
) -> Path:
    source = EXAMPLES_ROOT / rel
    workdir = tmp / "examples" / rel
    copy_example(source, workdir)
    enable_appkit(workdir)
    stage_example(workdir, python, tmp)

    build_dir = workdir / ".build"
    host_dir = build_dir / "apx_appkit_host"
    run_env = env(
        tmp,
        build_dir,
        python,
        {
            "APX_PYTHON_BRIDGE_CWD": str(build_dir),
            "APX_PYTHON_BRIDGE_PORT": str(BASE_BRIDGE_PORT + index),
            "DATABRICKS_APP_PORT": str(BASE_APP_PORT + index),
            "PORT": str(BASE_APP_PORT + index),
        },
    )
    node_modules = install_once(host_dir, install_source, run_env)

    proc = subprocess.Popen(
        ["npm", "start"],
        cwd=host_dir,
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_tcp(BASE_BRIDGE_PORT + index)
        wait_url(f"http://127.0.0.1:{BASE_APP_PORT + index}/health")
        invocation = post_json(
            f"http://127.0.0.1:{BASE_APP_PORT + index}/invocations",
            {"input": [{"role": "user", "content": "local smoke"}]},
        )
        if invocation.status != 200:
            raise RuntimeError(
                f"{rel}: /invocations returned {invocation.status}: {invocation.body}"
            )

        tool_status = None
        tool_equal = None
        tool_case = BRIDGE_TOOL_CASES.get(rel.as_posix())
        if tool_case is not None:
            direct = verify_direct_tool(
                build_dir,
                tmp,
                python,
                tool_case,
                BASE_APP_PORT + 1000 + index,
            )
            bridge = post_json(
                f"http://127.0.0.1:{BASE_BRIDGE_PORT + index}/_apx/internal/appkit/tools/{tool_case['name']}",
                {"args": tool_case["args"]},
                tool_case["headers"],
            )
            if direct.status != 200 or bridge.status != 200:
                raise RuntimeError(
                    f"{rel}: tool status direct={direct.status} bridge={bridge.status}"
                )
            compare_exact = tool_case.get("compare_exact", True)
            if compare_exact and direct.body != bridge.body:
                raise RuntimeError(f"{rel}: direct/AppKit bridge tool output differed")
            if tool_case["result_contains"] not in bridge.body:
                raise RuntimeError(f"{rel}: expected text missing from tool result")
            tool_status = bridge.status
            tool_equal = direct.body == bridge.body

        print(
            json.dumps(
                {
                    "example": rel.as_posix(),
                    "appkit_invocations_status": invocation.status,
                    "bridge_tool_status": tool_status,
                    "same_tool_result": tool_equal,
                },
                sort_keys=True,
            )
        )
    finally:
        terminate(proc)

    return node_modules


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--example", action="append", default=None)
    parser.add_argument("--keep-tmp", action="store_true")
    args = parser.parse_args()

    python = DEFAULT_PYTHON if DEFAULT_PYTHON.exists() else Path(sys.executable)
    examples = [Path(item) for item in args.example] if args.example else discover_examples()
    tmp = Path(tempfile.mkdtemp(prefix="apx-appkit-examples-"))
    print(json.dumps({"tmp": str(tmp), "examples": [item.as_posix() for item in examples]}))

    install_source: Path | None = None
    try:
        with fake_databricks():
            for index, rel in enumerate(examples):
                install_source = verify_example(rel, tmp, python, install_source, index)
    finally:
        if args.keep_tmp:
            print(json.dumps({"kept_tmp": str(tmp)}))
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
