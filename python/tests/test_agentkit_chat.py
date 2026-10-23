"""Vendored AgentKit chat app (databricks-agentbricks 0.5.0, LangGraph UI)."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "apx_agent"
ASSETS = SRC / "_static" / "agentkit_chat"
TEXT_FILES = ("index.html", "app.js", "styles.css")


def test_vendored_assets_present_and_non_empty() -> None:
    for name in (*TEXT_FILES, "markdown-it-15.0.2.min.js", "agentbricks-mark.png", "VENDORED.md"):
        path = ASSETS / name
        assert path.is_file() and path.stat().st_size > 100, name


def test_vendored_paths_are_rewritten_under_apx() -> None:
    for name in TEXT_FILES:
        text = (ASSETS / name).read_text(encoding="utf-8")
        leftovers = re.findall(r"""["'`(]/(?:ui-assets|api/demo|api/ui)\b""", text)
        assert leftovers == [], f"{name}: unrewritten AgentKit paths {leftovers}"
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "/_apx/assets/agentkit/app.js" in html
    assert "/_apx/api/demo/" in js and "/_apx/api/ui/config" in js
    # The invocation API stays where DurableAgentServer serves it.
    assert '"/api/invocations"' in js


from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def chat_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    from apx_agent._ui_agentkit_chat import mount_agentkit_chat

    monkeypatch.chdir(tmp_path)  # no agent.toml store bindings
    monkeypatch.delenv("AGENT_MEMORY_STORE", raising=False)
    monkeypatch.delenv("AGENT_SESSION_STORE", raising=False)
    app = FastAPI()
    mount_agentkit_chat(app, model="system.ai.test-model", ws=None)
    with TestClient(app) as client:  # lifespan adds the "/" page
        yield client


@pytest.mark.parametrize("path", ["/", "/_apx/chat", "/_apx/assets/agentkit/app.js"])
def test_chat_routes_serve_with_no_store(chat_client: TestClient, path: str) -> None:
    response = chat_client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) > 1000


def test_ui_config_reports_compiled_model(chat_client: TestClient) -> None:
    config = chat_client.get("/_apx/api/ui/config", params={"session_id": "s1"}).json()
    assert config["models"]["default"] == "system.ai.test-model"
    assert config["session_id"] == "s1"
    assert config["memory"]["enabled"] is False


def test_ui_config_requires_session_id(chat_client: TestClient) -> None:
    assert chat_client.get("/_apx/api/ui/config").status_code == 400


def test_memory_routes_503_without_store(chat_client: TestClient) -> None:
    response = chat_client.get("/_apx/api/demo/memory/entries", params={"session_id": "s1"})
    assert response.status_code == 503


def test_sessions_list_local_without_store(chat_client: TestClient) -> None:
    body = chat_client.get("/_apx/api/demo/sessions", params={"session_id": "s1"}).json()
    assert body["managed"] is False and body["current_session_id"] == "s1"


def test_session_items_is_honest_empty_transcript(chat_client: TestClient) -> None:
    body = chat_client.get("/_apx/api/demo/session/items", params={"session_id": "s1"}).json()
    assert body["session_items"] == [] and "#978" in body["note"]


def test_rank_models_pins_default_first() -> None:
    from apx_agent._ui_agentkit_chat import _rank_models

    assert _rank_models("b", ["c", "b", "a", "a"]) == ["b", "a", "c"]


def test_project_root_ui_keeps_slash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.staticfiles import StaticFiles
    from apx_agent._ui_agentkit_chat import mount_agentkit_chat

    monkeypatch.chdir(tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>project ui</h1>")
    app = FastAPI()
    mount_agentkit_chat(app, model="m", ws=None)
    app.mount("/", StaticFiles(directory=tmp_path / "dist", html=True), name="client")
    with TestClient(app) as client:
        assert "project ui" in client.get("/").text
        assert "/_apx/assets/agentkit/app.js" in client.get("/_apx/chat").text


def test_topology_bundle_carries_agentkit_palette() -> None:
    from ctk import Artifact, verify

    assets = SRC / "_static" / "topology" / "assets"
    css = sorted(assets.glob("index-*.css"))
    js = sorted(assets.glob("*.js"))
    assert len(css) == 1 and js, "rebuild the topology bundle (npm run build)"
    verify(
        Artifact(str(css[0]), min_bytes=1000, must_contain="#f1f3ff", must_not_contain="#11171C"),
        Artifact(str(css[0]), min_bytes=1000, must_contain="#1a73e8"),
        *(Artifact(str(p), min_bytes=1000, must_not_contain="#11171C") for p in js),
        # Discover edges must stay visible on the #f1f3ff canvas.
        *(Artifact(str(p), min_bytes=1000, must_not_contain="#d9dce6") for p in assets.glob("discover-*.js")),
    )


def test_data_inspector_launcher_is_current_durable_template() -> None:
    from apx_agent._project_gen import _START_HOST_CONTENT, _START_SERVER_CONTENT

    server = SRC.parents[1] / "examples" / "data-inspector" / "agent_server"
    assert (server / "start_server.py").read_text(encoding="utf-8") == _START_SERVER_CONTENT
    assert (server / "start_host.py").read_text(encoding="utf-8") == _START_HOST_CONTENT
    # The `agents deploy --target apps` launcher gate (cli.py) keys on this name.
    assert "DurableAgentServer" in _START_SERVER_CONTENT


def test_chat_uses_host_service_client_not_a_fresh_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    import databricks_agentkit
    from apx_agent._ui_agentkit_chat import mount_agentkit_chat

    def no_fresh_client() -> None:
        raise AssertionError("must reuse the host's service client")

    monkeypatch.setattr(databricks_agentkit, "workspace_client", no_fresh_client)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "databricks")
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "42")
    ws = MagicMock()
    ws.config.host = "https://example.cloud.databricks.com/"
    app = FastAPI()
    mount_agentkit_chat(app, model="m", ws=ws)
    with TestClient(app) as client:
        tracing = client.get("/_apx/api/ui/config", params={"session_id": "s"}).json()["tracing"]
    assert tracing["url"] == "https://example.cloud.databricks.com/ml/experiments/42"


def test_unused_session_write_routes_are_not_mounted(chat_client: TestClient) -> None:
    assert chat_client.post("/_apx/api/demo/session/items", params={"session_id": "s"},
                            json={"items": [{"role": "user", "content": "x"}]}).status_code in (404, 405)
    assert chat_client.get("/_apx/api/demo/session", params={"session_id": "s"}).status_code == 404


def test_data_inspector_deploys_via_apps_api_keeping_mcp_name() -> None:
    import tomllib

    root = SRC.parents[1] / "examples" / "data-inspector"
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deploy = pyproject["tool"]["apx"]["agent"]["deploy"]
    assert deploy["backend"] == "apps_api"  # Agent Bricks would require an agent-bricks- name
    module = root / deploy["entrypoint"].replace(".", "/")
    assert (module / "__main__.py").is_file(), "python -m <entrypoint> must start the host"
    assert "start_host" in (module / "__main__.py").read_text(encoding="utf-8")
    assert all((root / rel).exists() for rel in deploy["include"])
    # The deployed build fails on a hatch force-include the Apps API stage didn't copy.
    staged_by_default = {"agent.py", "tools.py"}
    forced = set(pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"])
    assert forced <= staged_by_default | set(deploy["include"]), forced
    assert not (root / "app.yml").exists(), "Apps API generates app.yaml and refuses a root manifest"
    # The declared app_name keeps mcp-; the agent name (data-triage's tool name) stays.
    assert pyproject["tool"]["apx"]["deploy"]["app_name"].startswith("mcp-")
    assert pyproject["tool"]["apx"]["agent"]["name"] == "data_inspector"
