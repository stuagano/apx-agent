"""The /_apx/tools inspector: runtime-derived source, in-place save, and the
page + routes that expose it (Tools tab of the shell, sub-tab of Edit)."""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from databricks.sdk.service.sql import StatementState
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard, AgentTool
from apx_agent._ui_tools import replace_function


class _FakeColumn:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSchema:
    def __init__(self, names: list[str]) -> None:
        self.columns = [_FakeColumn(n) for n in names]


class _FakeManifest:
    def __init__(self, names: list[str]) -> None:
        self.schema = _FakeSchema(names)


class _FakeResult:
    def __init__(self, rows) -> None:
        self.data_array = rows


class _FakeStatusObj:
    def __init__(self, state, error=None) -> None:
        self.state = state
        self.error = error


class _FakeStmtResponse:
    def __init__(self, names: list[str], rows) -> None:
        self.status = _FakeStatusObj(StatementState.SUCCEEDED)
        self.manifest = _FakeManifest(names)
        self.result = _FakeResult(rows)


class _FakeStatementExecution:
    def execute_statement(self, **kwargs):
        return _FakeStmtResponse(["database", "tableName", "isTemporary"], [["main", "alpha", False], ["main", "beta", False]])


class _FakeWarehouse:
    def __init__(self, wid: str, wh_type: str) -> None:
        self.id = wid
        self.warehouse_type = wh_type


class _FakeWarehouses:
    def list(self):
        return [_FakeWarehouse("wh-serverless-1", "PRO_SERVERLESS")]


class _FakeConfig:
    host = "https://test-workspace.cloud.databricks.com"


class _FakeWorkspaceClient:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.statement_execution = _FakeStatementExecution()
        self.warehouses = _FakeWarehouses()

TOOLS_SOURCE = '''"""Temp tool module for the inspector tests."""

CATALOG = "main"
SCHEMA = "default"
DEMO_MODE = False
_private = "not exposed"


def alpha(value: str) -> str:
    """Return alpha of the value."""
    return "alpha:" + value


def alpha_demo(value: str) -> str:
    """Synthetic alpha, used when DEMO_MODE is on."""
    return "fake:" + value


def beta(count: int) -> dict:
    """Return beta counts."""
    return {"count": count}
'''


@pytest.fixture
def tools_module(tmp_path: Path):
    """A real on-disk tool module — inspect.getsource needs actual file text."""
    path = tmp_path / "tools_probe.py"
    path.write_text(TOOLS_SOURCE)
    spec = importlib.util.spec_from_file_location("tools_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["tools_probe"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("tools_probe", None)


class _FakeAgent:
    def __init__(self, fns) -> None:
        self._tool_fns = fns


def _doc(fn) -> str:
    """``fn``'s docstring, or empty — ``AgentTool.description`` is a plain ``str``."""
    doc = inspect.getdoc(fn)
    return doc if doc else ""


def _ctx(fns, *, attach_agent: bool = True) -> AgentContext:
    config = AgentConfig(name="tools-test", model="fake-model")
    card = AgentCard(name="tools-test", description="", skills=[])
    tools = [
        AgentTool(
            name=fn.__name__,
            description=_doc(fn),
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        )
        for fn in fns
    ]
    return AgentContext(
        config=config,
        tools=tools,
        card=card,
        agent=_FakeAgent(fns) if attach_agent else None,  # type: ignore[arg-type]
    )


@pytest.fixture
def app(tools_module) -> FastAPI:
    a = FastAPI()
    a.state.agent_context = _ctx([tools_module.alpha, tools_module.beta])
    a.include_router(build_dev_ui_router())
    return a


@pytest.fixture
def client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── replace_function ─────────────────────────────────────────────────────────


class TestReplaceFunction:
    def test_rewrites_only_the_named_function(self):
        new = 'def alpha(value: str) -> str:\n    """Return alpha of the value."""\n    return "edited:" + value\n'
        updated, error = replace_function(TOOLS_SOURCE, "alpha", new)
        assert error is None
        assert updated is not None
        assert '"edited:" + value' in updated
        assert "def beta(count: int)" in updated        # neighbours untouched
        assert updated.count("def alpha(") == 1
        assert "def alpha_demo(" in updated

    def test_rejects_a_rename(self):
        updated, error = replace_function(TOOLS_SOURCE, "alpha", "def gamma(v):\n    return v\n")
        assert updated is None
        assert error is not None
        assert "exactly one top-level function named `alpha`" in error

    def test_rejects_a_syntax_error(self):
        updated, error = replace_function(TOOLS_SOURCE, "alpha", "def alpha(:\n")
        assert updated is None
        assert error is not None
        assert "Syntax error" in error

    def test_rejects_unknown_function(self):
        updated, error = replace_function(TOOLS_SOURCE, "nope", "def nope():\n    return 1\n")
        assert updated is None
        assert error is not None
        assert "not defined at the top level" in error


# ── Page + routes ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_page_renders_with_canonical_nav(client: AsyncClient):
    r = await client.get("/_apx/tools")
    assert r.status_code == 200
    assert "Tool inspector" in r.text
    assert 'href="/_apx/tools"' in r.text       # the nav advertises itself
    assert "apxDevFetch" in r.text              # shared write helper is present


@pytest.mark.asyncio
async def test_tools_page_preserves_backslash_n_in_inline_js(client: AsyncClient):
    r = await client.get("/_apx/tools")
    assert r.status_code == 200
    assert r"replace(/\n$/, '')" in r.text
    assert r"split('\n')" in r.text
    assert r"join('\n')" in r.text


@pytest.mark.asyncio
async def test_tools_page_has_a_run_panel(client: AsyncClient):
    r = await client.get("/_apx/tools")
    assert r.status_code == 200
    assert "_apx/replay/tool" in r.text
    assert 'id="run-args"' in r.text
    assert 'id="btn-run"' in r.text
    assert 'id="run-output"' in r.text
    assert "Run this tool" in r.text
    assert "unsaved edits are not run" in r.text
    assert "RUN_ARGS" in r.text
    assert "Ctrl/⌘+Enter" in r.text


@pytest.mark.asyncio
async def test_tools_page_renders_tool_output_as_a_table(client: AsyncClient):
    r = await client.get("/_apx/tools")
    assert r.status_code == 200
    assert "renderToolOutput" in r.text          # success path uses the renderer
    assert "tableShape" in r.text                 # tabular detection
    assert 'table class="out"' in r.text          # the table itself
    assert "Raw JSON" in r.text                   # raw toggle stays available
    assert '<div id="run-output"></div>' in r.text  # container holds markup, not text
    assert '<pre id="run-output">' not in r.text


def _tools_page_script() -> str:
    from apx_agent._ui_tools import _PAGE
    from apx_agent._ui_table import TABLE_JS

    match = re.search(r"<script[^>]*>(.*?)</script>", _PAGE, flags=re.S)
    assert match, "the tools page has an inline script"
    return match.group(1).replace("{APX_TABLE_JS}", TABLE_JS)


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_tool_output_renderer_behavior(tmp_path: Path):
    """Exercise the real renderer in node: tables for tabular JSON, escaping,
    caps, and a plain-JSON fallback."""
    prelude = """
global.document = { getElementById: () => ({ addEventListener() {},
  classList: { add() {}, remove() {} }, style: {}, textContent: '', innerHTML: '' }) };
global.window = {};
global.fetch = async () => ({ ok: false, status: 500, text: async () => '' });
"""
    checks = """
const R = {};
const rowsPayload = { count: 2, source: "live", revisions: [
  { revision_id: "r1", field_count: 3 }, { revision_id: "r2", field_count: 4 }] };
const rowsHtml = renderToolOutput(JSON.stringify(rowsPayload));
R.rows_table = rowsHtml.includes('table class="out"');
R.rows_label = rowsHtml.includes('rows: revisions');
R.rows_header = rowsHtml.includes('<th>revision_id</th>');
R.rows_cell = rowsHtml.includes('<td>r1</td>');
R.rows_raw = rowsHtml.includes('Raw JSON');
R.rows_meta = rowsHtml.includes('count = 2') && rowsHtml.includes('source = live');

const nested = renderToolOutput(JSON.stringify({ fields: [
  { target_field: "x", source_columns: ["a", "b"], nullable: true, verdict: null }] }));
R.nested_array_cell = nested.includes('a, b');
R.nested_bool_cell = nested.includes('<td>true</td>');
R.nested_null_cell = nested.includes('—');

const plain = renderToolOutput(JSON.stringify({ a: 1, b: { c: 2 } }));
R.plain_no_table = !plain.includes('<table');
R.plain_has_json = plain.includes('&quot;a&quot;: 1');
R.plain_no_raw_toggle = !plain.includes('Raw JSON');

const xss = renderToolOutput(JSON.stringify({ fields: [
  { target_field: "<script>alert(1)</script>" }] }));
R.xss_escaped = xss.includes('&lt;script&gt;') && !xss.includes('<script>alert');

const wide = { fields: [{}] };
for (let i = 0; i < 20; i++) wide.fields[0]['col' + i] = i;
R.col_cap = renderToolOutput(JSON.stringify(wide)).includes('+6 more column(s) not shown');

const long = { fields: [] };
for (let i = 0; i < 205; i++) long.fields.push({ i });
R.row_cap = renderToolOutput(JSON.stringify(long)).includes('+5 more not shown');

R.text_fallback = renderToolOutput('not json') === 'not json';
R.scalar_shape_ok = tableShape({}) === null && tableShape([]) === null;

console.log("RESULT:" + JSON.stringify(R));
"""
    script = prelude + "\n" + _tools_page_script() + "\n" + checks
    path = tmp_path / "renderer_probe.js"
    path.write_text(script)
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert line, proc.stdout[-2000:]
    result = json.loads(line[-1][len("RESULT:"):])
    failed = [k for k, v in result.items() if not v]
    assert not failed, f"renderer checks failed: {failed}"


@pytest.mark.asyncio
async def test_tools_page_includes_connection_banner_strings(client: AsyncClient):
    r = await client.get("/_apx/tools")
    assert r.status_code == 200
    # The two states the page decides client-side are literals in the template.
    assert "DEMO — Databricks UC not connected" in r.text
    assert "LIVE configured — Databricks UC unreachable" in r.text
    assert "renderConnectionBanner" in r.text
    assert "DATA.connection" in r.text
    # Status copy is semantic; the raw flag name is not user-facing.
    assert "DEMO_MODE=" not in r.text
    assert "This deployment is currently using the " in r.text
    assert "not executed in this deployment" in r.text


@pytest.mark.asyncio
async def test_tools_list_includes_connection_status(
    client: AsyncClient, tools_module, monkeypatch: pytest.MonkeyPatch
):
    import apx_agent._ui_tools as ui_tools

    monkeypatch.setattr(ui_tools, "WorkspaceClient", _FakeWorkspaceClient)
    r = await client.get("/_apx/tools/list")
    assert r.status_code == 200
    data = r.json()
    assert data["connection"]["connected"] is True
    assert data["connection"]["workspace"] == "https://test-workspace.cloud.databricks.com"
    assert data["connection"]["catalog"] == "main"
    assert data["connection"]["schema"] == "default"
    assert data["connection"]["tables"] == ["alpha", "beta"]
    assert data["connection"]["warehouse_id"] == "wh-serverless-1"
    assert data["connection"]["headline"] == "Connected — Databricks UC reachable"
    assert data["connection"]["error"] == ""


@pytest.mark.asyncio
async def test_tools_list_derives_source_from_the_defining_file(
    client: AsyncClient, tools_module
):
    r = await client.get("/_apx/tools/list")
    assert r.status_code == 200
    data = r.json()
    assert data["how"] == "agent"
    names = [t["name"] for t in data["tools"]]
    assert names == ["alpha", "beta"]           # wiring order, not alphabetical

    alpha = data["tools"][0]
    assert alpha["file"].endswith("tools_probe.py")
    assert alpha["line"] == TOOLS_SOURCE.split("def alpha(")[0].count("\n") + 1
    assert 'return "alpha:" + value' in alpha["source"]
    assert alpha["llm_description"] == "Return alpha of the value."
    assert alpha["file_writable"] is True
    # The synthetic twin lives in the same module and is surfaced, not merged.
    assert alpha["demo_name"] == "alpha_demo"
    assert 'return "fake:" + value' in alpha["demo_source"]
    assert data["tools"][1]["demo_name"] == ""
    # Runtime constants come from the tool module, not a hardcoded list.
    assert data["config"]["CATALOG"] == "main"
    assert data["config"]["SCHEMA"] == "default"
    assert "_private" not in data["config"]
    assert data["status"]["agent_context"] is True
    assert data["warnings"] == []


@pytest.mark.asyncio
async def test_tools_list_recovers_when_no_agent_is_attached(client: AsyncClient, app: FastAPI):
    app.state.agent_context = _ctx([], attach_agent=False)
    # Re-point the context at the same tool names without the compiled agent.
    app.state.agent_context.tools = [
        AgentTool(name="alpha", description="Return alpha of the value.", input_schema={}),
    ]
    r = await client.get("/_apx/tools/list")
    assert r.status_code == 200
    data = r.json()
    assert data["how"] == "module"
    assert [t["name"] for t in data["tools"]] == ["alpha"]
    assert any("recovered" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_tools_file_downloads_the_defining_module(client: AsyncClient):
    r = await client.get("/_apx/tools/file?name=alpha")
    assert r.status_code == 200
    assert 'def alpha(value: str) -> str:' in r.text
    assert "tools_probe.py" in r.headers["content-disposition"]


@pytest.mark.asyncio
async def test_tools_file_without_source_is_404(client: AsyncClient, app: FastAPI):
    app.state.agent_context = _ctx([])
    r = await client.get("/_apx/tools/file")
    assert r.status_code == 404
    assert r.json()["ok"] is False


# ── Save ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_rewrites_the_function_and_reports_container_only(
    client: AsyncClient, tools_module, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    path = Path(tools_module.__file__)
    edited = (
        'def alpha(value: str) -> str:\n'
        '    """Return alpha of the value."""\n'
        '    return "edited:" + value\n'
    )
    r = await client.post(
        "/_apx/tools/save",
        json={"name": "alpha", "source": edited, "expected_original": inspect.getsource(tools_module.alpha)},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["durable"] is False              # no app name → container only, and said so
    assert data["container_path"] == str(path)
    assert data["restart_required"] is True
    assert any("container" in note for note in data["notes"])
    assert '"edited:" + value' in path.read_text()
    assert "def beta(count: int)" in path.read_text()
    # linecache was refreshed, so the page serves the new text immediately.
    assert '"edited:" + value' in inspect.getsource(tools_module.alpha)


@pytest.mark.asyncio
async def test_save_rejects_stale_original(client: AsyncClient, tools_module):
    before = Path(tools_module.__file__).read_text()
    r = await client.post(
        "/_apx/tools/save",
        json={
            "name": "alpha",
            "source": "def alpha(value: str) -> str:\n    return 'x'\n",
            "expected_original": "something else entirely",
        },
    )
    assert r.status_code == 409
    assert "changed on disk" in r.json()["error"]
    assert Path(tools_module.__file__).read_text() == before


@pytest.mark.asyncio
async def test_save_unknown_tool_is_404(client: AsyncClient):
    r = await client.post("/_apx/tools/save", json={"name": "ghost", "source": "def ghost():\n    return 1\n"})
    assert r.status_code == 404
    assert r.json()["ok"] is False


@pytest.mark.asyncio
async def test_save_missing_body_field_is_422(client: AsyncClient):
    r = await client.post("/_apx/tools/save", json={"name": "alpha"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_save_inherits_the_dev_write_guard(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """On a deployed App the POST must be gated by Apps SSO / the operator
    token, exactly like every other dev-UI write."""
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    r = await client.post("/_apx/tools/save", json={"name": "alpha", "source": "def alpha():\n    return 1\n"})
    assert r.status_code == 403


# ── Discovery from the shell + Edit page ─────────────────────────────────────


@pytest.mark.asyncio
async def test_unified_shell_has_a_tools_tab(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/agent")
    assert r.status_code == 200
    assert 'data-tab="tools"' in r.text
    assert 'data-src="/_apx/tools"' in r.text


@pytest.mark.asyncio
async def test_chat_tools_tab_embeds_the_live_inspector(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/chat")
    assert r.status_code == 200
    assert 'id="tab-tools"' in r.text
    assert 'id="tools-embed"' in r.text
    assert "frame.src = '/_apx/tools';" in r.text
    assert 'Open full page ↗' in r.text


@pytest.mark.asyncio
async def test_edit_page_has_a_tools_subtab_that_embeds_the_inspector(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    import apx_agent._dev as _dev

    monkeypatch.setattr(_dev, "_find_agent_router_path", lambda: None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/edit")
    assert r.status_code == 200
    assert 'id="subtab-tools"' in r.text
    assert "'/_apx/tools'" in r.text          # iframe src built on first open
    assert 'id="subtab-schemas"' in r.text
