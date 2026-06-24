from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent import _dev
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard


BASE_AGENT_ROUTER = '''\
from typing import Any
from databricks.sdk import WorkspaceClient
from databricks_langchain import ChatDatabricks
from databricks_langchain.genie import GenieAgent
from databricks_langchain.uc_ai import UCFunctionToolkit
from langgraph.prebuilt import create_react_agent
from unitycatalog.ai.langchain.toolkit import UnityCatalogTool
from unitycatalog.ai.core.databricks import DatabricksFunctionClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
from databricks.sdk.service.serving import ChatCompletionRequest

class Dependencies:
    Client = WorkspaceClient

def existing_tool(ws: Dependencies.Client, value: str) -> str:
    """Existing test helper."""
    return value

agent = create_react_agent(
    model=ChatDatabricks(endpoint="test-model"),
    tools=[existing_tool],
    prompt="Initial instructions",
)
'''


class _FakeConfig:
    host = "https://example.cloud.databricks.com"

    def authenticate(self) -> dict[str, str]:
        return {"Authorization": "Bearer fake"}


class _FakeWorkspaceClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.config = _FakeConfig()
        self.current_user = SimpleNamespace(me=self._current_user_me)
        self.catalogs = SimpleNamespace(list=self._catalogs_list)
        self.schemas = SimpleNamespace(list=self._schemas_list)
        self.tables = SimpleNamespace(list=self._tables_list)
        self.warehouses = SimpleNamespace(list=self._warehouses_list)
        self.vector_search_endpoints = SimpleNamespace(list_endpoints=self._vs_list_endpoints)
        self.vector_search_indexes = SimpleNamespace(
            list_indexes=self._vs_list_indexes,
            get_index=self._vs_get_index,
        )
        self.apps = SimpleNamespace(get=lambda _name: SimpleNamespace(default_source_code_path=None))
        self.workspace = SimpleNamespace(upload=lambda *_args, **_kwargs: None)

    def _maybe_fail(self) -> None:
        if self.fail:
            raise RuntimeError("workspace boom")

    def _current_user_me(self) -> SimpleNamespace:
        self._maybe_fail()
        return SimpleNamespace(user_name="dev@example.com")

    def _catalogs_list(self) -> list[SimpleNamespace]:
        self._maybe_fail()
        return [SimpleNamespace(name="samples"), SimpleNamespace(name="main")]

    def _schemas_list(self, *, catalog_name: str) -> list[SimpleNamespace]:
        self._maybe_fail()
        assert catalog_name == "main"
        return [SimpleNamespace(name="default"), SimpleNamespace(name="information_schema")]

    def _tables_list(self, *, catalog_name: str, schema_name: str) -> list[SimpleNamespace]:
        self._maybe_fail()
        assert (catalog_name, schema_name) == ("main", "default")
        return [SimpleNamespace(name="customers"), SimpleNamespace(name="orders")]

    def _warehouses_list(self) -> list[SimpleNamespace]:
        self._maybe_fail()
        return [SimpleNamespace(id="wh-1", name="Small Warehouse", state=SimpleNamespace(value="RUNNING"))]

    def _vs_list_endpoints(self) -> list[SimpleNamespace]:
        self._maybe_fail()
        return [SimpleNamespace(name="vs-endpoint", endpoint_status=SimpleNamespace(state=SimpleNamespace(value="ONLINE")))]

    def _vs_list_indexes(self, *, endpoint_name: str) -> SimpleNamespace:
        self._maybe_fail()
        assert endpoint_name == "vs-endpoint"
        return SimpleNamespace(vector_indexes=[SimpleNamespace(name="main.default.customer_idx")])

    def _vs_get_index(self, *, index_name: str) -> SimpleNamespace:
        self._maybe_fail()
        assert index_name == "main.default.customer_idx"
        return SimpleNamespace(
            status=SimpleNamespace(ready=True),
            delta_sync_index_spec=SimpleNamespace(
                source_table="main.default.customers",
                embedding_source_columns=[SimpleNamespace(name="content")],
            ),
        )


@dataclass
class _MemoryRow:
    id: str
    content: str
    namespace: str | None
    updated_at: str


class _MemoryStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def list(self, _filter: Any) -> list[_MemoryRow]:
        if self.fail:
            raise RuntimeError("memory boom")
        return [
            _MemoryRow("older", "older memory", "ns", "2024-01-01T00:00:00Z"),
            _MemoryRow("newer", "newer memory", "ns", "2024-01-02T00:00:00Z"),
        ]


class _FakeAgent:
    def __init__(self, *, memory_store: _MemoryStore | None = None) -> None:
        self._apx_memory_store = memory_store
        self._apx_memory_principal = "principal-1"
        self._apx_memory_namespace = "ns"


def _make_ctx(*, agent: Any | None = None, model: str = "fake-model") -> AgentContext:
    config = AgentConfig(
        name="dev-ui-test",
        model=model,
        instructions="Use warehouse data carefully.",
    )
    card = AgentCard(name="dev-ui-test", description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=agent)  # type: ignore[arg-type]


@pytest.fixture
def agent_router_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "agent_router.py"
    path.write_text(BASE_AGENT_ROUTER)
    monkeypatch.setattr(_dev, "_find_agent_router_path", lambda: path)
    return path


@pytest.fixture
def app(agent_router_path: Path) -> FastAPI:
    fastapi_app = FastAPI()
    fastapi_app.state.agent_context = _make_ctx(agent=_FakeAgent())
    fastapi_app.state.workspace_client = _FakeWorkspaceClient()
    fastapi_app.include_router(build_dev_ui_router())
    return fastapi_app


@pytest.fixture
def client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def fake_databricks_openai(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    outputs: list[str] = []
    module = ModuleType("databricks_openai")

    class _FakeCompletions:
        async def create(self, **_kwargs: Any) -> SimpleNamespace:
            raw = outputs.pop(0)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw))]
            )

    class _FakeAsyncDatabricksOpenAI:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    module.AsyncDatabricksOpenAI = _FakeAsyncDatabricksOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks_openai", module)
    return outputs


class _FakeSuggestHTTPClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def __aenter__(self) -> "_FakeSuggestHTTPClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: self.payload,
        )


def _patch_suggest_http_client(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    real_async_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> Any:
        if "transport" in kwargs:
            return real_async_client(*args, **kwargs)
        return _FakeSuggestHTTPClient(payload)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_tools_suggest_returns_model_spec_without_writing(
    client: AsyncClient,
    agent_router_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = agent_router_path.read_text()
    payload = {
        "choices": [
            {
                "message": {
                    "content": '{"name":"lookup_customer","description":"Lookup a customer.","params":[{"name":"customer_id","type":"str","desc":"Customer id"}],"returns":"str","body":"    return customer_id"}'
                }
            }
        ]
    }
    _patch_suggest_http_client(monkeypatch, payload)

    response = await client.post("/_apx/tools/suggest", json={"prompt": "lookup customer"})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["spec"]["name"] == "lookup_customer"
    assert agent_router_path.read_text() == before


@pytest.mark.asyncio
async def test_tools_suggest_handles_non_json_model_output(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"choices": [{"message": {"content": "not json"}}]}
    _patch_suggest_http_client(monkeypatch, payload)

    response = await client.post("/_apx/tools/suggest", json={"prompt": "lookup customer"})

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "Model returned non-JSON — try rephrasing"}


@pytest.mark.asyncio
@pytest.mark.parametrize("route,body", [
    ("/_apx/setup/create-tool", {"description": "add lookup tool"}),
    ("/_apx/wizard/generate-tools", {"description": "add lookup tool"}),
])
async def test_codegen_chained_tool_routes_write_parseable_agent_router(
    client: AsyncClient,
    agent_router_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    body: dict[str, str],
) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": '{"name":"lookup_customer","description":"Lookup a customer.","params":[{"name":"customer_id","type":"str","desc":"Customer id"}],"returns":"str","body":"    return customer_id"}'
                }
            }
        ]
    }
    _patch_suggest_http_client(monkeypatch, payload)

    response = await client.post(route, json=body)

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["tool_name"] == "lookup_customer"
    updated = agent_router_path.read_text()
    ast.parse(updated)
    assert "def lookup_customer" in updated


@pytest.mark.asyncio
@pytest.mark.parametrize("route,body", [
    ("/_apx/setup/create-tool", {"description": "add lookup tool"}),
    ("/_apx/wizard/generate-tools", {"description": "add lookup tool"}),
])
async def test_codegen_chained_tool_routes_propagate_suggest_ok_false(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    body: dict[str, str],
) -> None:
    payload = {"choices": [{"message": {"content": "not json"}}]}
    _patch_suggest_http_client(monkeypatch, payload)

    response = await client.post(route, json=body)

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "error" in response.json()


@pytest.mark.asyncio
async def test_codegen_chained_tool_route_handles_malformed_spec_from_new(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": '{"name":"bad_tool","description":"Bad.","params":[],"returns":"str","body":"    if True print(1)"}'
                }
            }
        ]
    }
    _patch_suggest_http_client(monkeypatch, payload)

    response = await client.post("/_apx/setup/create-tool", json={"description": "bad tool"})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert "Syntax error" in data["error"]


@pytest.mark.asyncio
async def test_setup_wire_agent_returns_parseable_tool_selection(
    client: AsyncClient,
    fake_databricks_openai: list[str],
) -> None:
    fake_databricks_openai.append('{"tools":["existing_tool"],"instructions":"Use existing_tool."}')

    response = await client.post(
        "/_apx/setup/wire-agent",
        json={"agent_name": "agent", "behavior": "Echo values"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data == {"ok": True, "tools": ["existing_tool"], "instructions": "Use existing_tool."}
    ast.parse(f"agent_tools = {data['tools']!r}\nagent_instructions = {data['instructions']!r}\n")


@pytest.mark.asyncio
async def test_setup_wire_agent_handles_malformed_model_output(
    client: AsyncClient,
    fake_databricks_openai: list[str],
) -> None:
    fake_databricks_openai.append("not json")

    response = await client.post(
        "/_apx/setup/wire-agent",
        json={"agent_name": "agent", "behavior": "Echo values"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert "Expecting value" in data["error"]


@pytest.mark.asyncio
async def test_edit_preview_extracts_schemas_without_writing_target(
    client: AsyncClient,
    agent_router_path: Path,
) -> None:
    before = agent_router_path.read_text()
    source = before + '\n\ndef preview_only(ws: Dependencies.Client, q: str) -> str:\n    """Preview only."""\n    return q\n'

    response = await client.post("/_apx/edit/preview", json={"source": source})

    assert response.status_code == 200
    data = response.json()
    assert any(item["name"] == "preview_only" for item in data)
    assert "diff" not in data
    assert agent_router_path.read_text() == before


@pytest.mark.asyncio
async def test_setup_discovery_routes_return_json_shapes(client: AsyncClient) -> None:
    catalogs = await client.get("/_apx/setup/catalogs")
    schemas = await client.get("/_apx/setup/schemas", params={"catalog": "main"})
    tables = await client.get("/_apx/setup/tables", params={"catalog": "main", "schema": "default"})
    warehouses = await client.get("/_apx/setup/warehouses")
    indexes = await client.get("/_apx/setup/vs-indexes")

    assert catalogs.status_code == 200 and catalogs.json() == ["main", "samples"]
    assert schemas.status_code == 200 and schemas.json() == ["default"]
    assert tables.status_code == 200 and tables.json() == ["customers", "orders"]
    assert warehouses.status_code == 200 and warehouses.json() == [
        {"id": "wh-1", "name": "Small Warehouse", "state": "RUNNING"}
    ]
    assert indexes.status_code == 200
    assert indexes.json() == [
        {
            "endpoint": "vs-endpoint",
            "endpoint_state": "ONLINE",
            "index": "main.default.customer_idx",
            "source_table": "main.default.customers",
            "ready": True,
            "columns": ["content"],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("route,params", [
    ("/_apx/setup/catalogs", {}),
    ("/_apx/setup/schemas", {"catalog": "main"}),
    ("/_apx/setup/tables", {"catalog": "main", "schema": "default"}),
    ("/_apx/setup/warehouses", {}),
    ("/_apx/setup/vs-indexes", {}),
])
async def test_setup_discovery_routes_degrade_to_error_json(
    app: FastAPI,
    route: str,
    params: dict[str, str],
) -> None:
    app.state.workspace_client = _FakeWorkspaceClient(fail=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as failing_client:
        response = await failing_client.get(route, params=params)

    if route == "/_apx/setup/vs-indexes":
        assert response.status_code == 200
        assert response.json() == [{"error": "Could not list endpoints: workspace boom"}]
    else:
        assert response.status_code == 500
        assert response.json() == {"error": "workspace boom"}


@pytest.mark.asyncio
async def test_workspace_context_returns_shape_and_degrades_user_lookup(app: FastAPI) -> None:
    app.state.workspace_client = _FakeWorkspaceClient(fail=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as failing_client:
        response = await failing_client.get("/_apx/workspace-context")

    assert response.status_code == 200
    data = response.json()
    assert data["host"] == "https://example.cloud.databricks.com"
    assert data["user"] == "unknown"
    assert data["resources"] == []
    assert data["used_catalogs"] == []
    assert data["used_schemas"] == []


@pytest.mark.asyncio
async def test_memories_returns_recent_rows_and_degrades_on_store_failure(app: FastAPI) -> None:
    app.state.agent_context = _make_ctx(agent=_FakeAgent(memory_store=_MemoryStore()))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ok_client:
        ok_response = await ok_client.get("/_apx/memories")

    assert ok_response.status_code == 200
    assert [row["id"] for row in ok_response.json()] == ["newer", "older"]

    app.state.agent_context = _make_ctx(agent=_FakeAgent(memory_store=_MemoryStore(fail=True)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as failing_client:
        failing_response = await failing_client.get("/_apx/memories")

    assert failing_response.status_code == 200
    assert failing_response.json() == []


@pytest.mark.asyncio
async def test_setup_generate_and_apply_instructions_routes(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_generate(
        _ws: Any,
        _ctx: Any,
        catalog: str,
        schema: str,
        warehouse_id: str,
    ) -> str:
        return f"Use {catalog}.{schema} through {warehouse_id}."

    persisted: list[str] = []
    monkeypatch.setattr(_dev, "_generate_agent_instructions", fake_generate)
    monkeypatch.setattr(
        _dev,
        "_persist_instructions",
        lambda _ctx, instructions, ws_client=None: persisted.append(instructions),
    )

    generated = await client.post(
        "/_apx/setup/generate-instructions",
        json={"catalog": "main", "schema": "default", "warehouse_id": "wh-1"},
    )
    applied = await client.post(
        "/_apx/setup/apply-instructions",
        json={"instructions": "Pinned instructions"},
    )
    empty = await client.post("/_apx/setup/apply-instructions", json={"instructions": ""})

    assert generated.status_code == 200
    assert generated.json() == {"ok": True, "instructions": "Use main.default through wh-1."}
    assert applied.status_code == 200 and applied.json() == {"ok": True}
    assert empty.status_code == 200 and empty.json()["ok"] is False
    assert persisted == ["Use main.default through wh-1.", "Pinned instructions"]


@pytest.mark.asyncio
async def test_setup_generate_instructions_degrades_to_error_json(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_generate(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("instruction boom")

    monkeypatch.setattr(_dev, "_generate_agent_instructions", failing_generate)

    with pytest.raises(RuntimeError, match="instruction boom"):
        await client.post(
            "/_apx/setup/generate-instructions",
            json={"catalog": "main", "schema": "default", "warehouse_id": "wh-1"},
        )


# ── PR-R1: un-hidden + typed setup-discovery reads ───────────────────────────


@pytest.mark.asyncio
async def test_r1_setup_discovery_routes_are_published_to_openapi(client: AsyncClient) -> None:
    """Every PR-R1 read route is now in the native OpenAPI schema (un-hidden).

    The routes that also have a (still-hidden) ``POST`` sibling — ``setup/agents``
    and ``setup/agent-pattern`` — must expose the ``get`` operation only.
    """
    spec = (await client.get("/openapi.json")).json()
    paths = spec["paths"]
    for route in (
        "/_apx/setup/catalogs",
        "/_apx/setup/schemas",
        "/_apx/setup/tables",
        "/_apx/setup/warehouses",
        "/_apx/setup/agents",
        "/_apx/setup/tools",
        "/_apx/tools/schema",
        "/_apx/setup/vs-indexes",
        "/_apx/setup/agent-pattern",
        "/_apx/setup/probe-json",
    ):
        assert route in paths, f"{route} missing from OpenAPI schema (still hidden?)"
        assert "get" in paths[route], f"{route} GET not published"
    # The source-mutating POST siblings stay hidden.
    assert "post" not in paths["/_apx/setup/agents"]
    assert "post" not in paths["/_apx/setup/agent-pattern"]


@pytest.mark.asyncio
async def test_r1_setup_tools_returns_typed_tool_list(client: AsyncClient) -> None:
    response = await client.get("/_apx/setup/tools")
    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "existing_tool",
            "description": "Existing test helper.",
            "params": [{"name": "value", "type": "string"}],
        }
    ]


@pytest.mark.asyncio
async def test_r1_agent_pattern_get_returns_wrapper_type(client: AsyncClient) -> None:
    response = await client.get("/_apx/setup/agent-pattern")
    assert response.status_code == 200
    # BASE_AGENT_ROUTER's ``agent`` is a bare create_react_agent (no wrapper).
    assert response.json() == {"type": "Agent"}


@pytest.mark.asyncio
async def test_r1_tools_schema_degrades_when_agent_router_unconfigured(client: AsyncClient) -> None:
    """No imported ``backend.agent_router`` → CATALOG/SCHEMA/WAREHOUSE_ID unset →
    a 200 ``{ok: false}`` degrade that bypasses the response model."""
    response = await client.get("/_apx/tools/schema")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert "CATALOG/SCHEMA/WAREHOUSE_ID" in data["error"]


@pytest.mark.asyncio
async def test_r1_tools_schema_success_serialises_schema_alias_on_wire(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the agent_router globals set but no live warehouse query reachable
    (the fake client has no ``statement_execution``), the handler falls through
    to an empty-but-typed ``{ok, catalog, schema, tables}`` success — proving the
    ``ToolSchemaResponse`` model validates and that its aliased field lands on
    the wire as ``schema`` (not ``schema_``)."""
    fake_ar = ModuleType("proj.backend.agent_router")
    fake_ar.CATALOG = "main"  # type: ignore[attr-defined]
    fake_ar.SCHEMA = "default"  # type: ignore[attr-defined]
    fake_ar.WAREHOUSE_ID = "wh-1"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "proj.backend.agent_router", fake_ar)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/_apx/tools/schema")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["catalog"] == "main"
    assert data["schema"] == "default"  # aliased field, not "schema_"
    assert data["tables"] == {}


# ── PR-R3: un-hidden + typed orphan JSON reads ───────────────────────────────


@pytest.mark.asyncio
async def test_r3_orphan_read_routes_published_to_openapi(client: AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    for route in (
        "/_apx/openapi.json",
        "/_apx/probe/checks",
        "/_apx/topology.json",
        "/_apx/workspace-context",
    ):
        assert route in paths and "get" in paths[route], f"{route} not published"
    inspect = next(p for p in paths if p.startswith("/_apx/topology/inspect/"))
    assert "get" in paths[inspect]


@pytest.mark.asyncio
async def test_r3_apx_openapi_json_returns_curated_spec(client: AsyncClient) -> None:
    r = await client.get("/_apx/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.")
    assert "paths" in spec


@pytest.mark.asyncio
async def test_r3_topology_json_preserves_rootid_and_agentname(client: AsyncClient) -> None:
    """``response_model=TopologyResponse`` must NOT strip the ``rootId`` /
    ``agentName`` top-level keys the react-flow UI needs."""
    r = await client.get("/_apx/topology.json")
    assert r.status_code == 200
    g = r.json()
    assert set(g) >= {"rootId", "agentName", "nodes", "edges"}
    assert g["rootId"]  # non-empty entry-point id
    assert isinstance(g["nodes"], list) and isinstance(g["edges"], list)


@pytest.mark.asyncio
async def test_r3_topology_inspect_round_trips_a_real_node(client: AsyncClient) -> None:
    root_id = (await client.get("/_apx/topology.json")).json()["rootId"]
    r = await client.get(f"/_apx/topology/inspect/{root_id}")
    assert r.status_code == 200
    assert r.json()["id"] == root_id


@pytest.mark.asyncio
async def test_r3_topology_inspect_unknown_node_is_404(client: AsyncClient) -> None:
    r = await client.get("/_apx/topology/inspect/nope:nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_r3_topology_routes_degrade_without_context(app: FastAPI) -> None:
    app.state.agent_context = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/_apx/topology.json")).status_code == 503
        assert (await ac.get("/_apx/topology/inspect/agent:root")).status_code == 503


# ── PR-W2a: typed + un-hidden codegen file-ops writes ────────────────────────


@pytest.mark.asyncio
async def test_w2a_file_ops_routes_published_to_openapi(client: AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert "get" in paths["/_apx/edit"]      # HTML shell, un-hidden
    assert "post" in paths["/_apx/edit"]
    assert "post" in paths["/_apx/edit/preview"]
    tool_del = next(p for p in paths if p.startswith("/_apx/tools/") and "{fn_name" in p)
    assert "delete" in paths[tool_del]


@pytest.mark.asyncio
async def test_w2a_edit_save_writes_source_and_reports_no_restart(
    client: AsyncClient, agent_router_path: Path
) -> None:
    new_src = "agent = None  # rewritten by test\n"
    r = await client.post("/_apx/edit", json={"content": new_src})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "restart_required": False}
    assert agent_router_path.read_text() == new_src  # read-after-write


@pytest.mark.asyncio
async def test_w2a_edit_save_rejects_syntax_error_without_writing(
    client: AsyncClient, agent_router_path: Path
) -> None:
    before = agent_router_path.read_text()
    r = await client.post("/_apx/edit", json={"content": "def oops(:\n"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False and "Syntax error" in data["error"]
    assert agent_router_path.read_text() == before  # unchanged


@pytest.mark.asyncio
async def test_w2a_edit_save_missing_content_is_422(client: AsyncClient) -> None:
    # Missing required field → FastAPI 422 (policy #1), not a handler 400.
    assert (await client.post("/_apx/edit", json={})).status_code == 422


@pytest.mark.asyncio
async def test_w2a_delete_tool_removes_function_from_source(
    client: AsyncClient, agent_router_path: Path
) -> None:
    assert "def existing_tool" in agent_router_path.read_text()
    r = await client.delete("/_apx/tools/existing_tool")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert "def existing_tool" not in agent_router_path.read_text()  # read-after-write


@pytest.mark.asyncio
async def test_w2a_delete_unknown_tool_returns_ok_false(client: AsyncClient) -> None:
    r = await client.delete("/_apx/tools/nonexistent_tool")
    assert r.status_code == 200
    assert r.json()["ok"] is False
