# apx-builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Databricks App that lets a rep describe an agent in plain English and receive a live deployed Databricks App URL within 15 minutes, with no coding required.

**Architecture:** A FastAPI + React app living in `python/examples/apx-builder/` in the apx-agent repo. The app is itself an `apx-agent` project — the builder's FastAPI app is created via `create_app(agent)`. The agent has four tools (discover_tables, scaffold_project, deploy_agent, poll_deployment) and a system prompt that drives a 3–5 question discovery conversation followed by fully autonomous build + deploy.

**Tech Stack:** `apx-agent` (agent runtime + OBO auth), `databricks-sdk` (Workspace/Apps APIs), `httpx` (health check polling), React + TypeScript + Vite (chat UI), `pytest` (tests)

**Implementation repo:** `~/Documents/apx-agent/` — all file paths below are relative to `python/examples/apx-builder/` inside that repo.

---

## File Map

| File | Responsibility |
|------|---------------|
| `pyproject.toml` | Project deps, apx metadata, pytest config |
| `app.yml` | Databricks Apps deploy command |
| `app.py` | Agent definition + static file serving |
| `system_prompt.py` | Discovery flow instructions + apx-agent knowledge |
| `tools/__init__.py` | Empty |
| `tools/discover_tables.py` | `search_tables()` (UC catalog search) + `list_genie_spaces()` |
| `tools/scaffold_project.py` | `_generate_files()` (pure) + `_upload_files()` + `scaffold_project()` |
| `tools/deploy_agent.py` | `deploy_agent()` (create-if-missing + deploy) |
| `tools/poll_deployment.py` | `poll_deployment()` (two-stage: API state + HTTP health) |
| `tests/test_discover_tables.py` | Unit tests for table/Genie discovery |
| `tests/test_scaffold_project.py` | Unit tests for file generation + upload |
| `tests/test_deploy_agent.py` | Unit tests for app create + deploy |
| `tests/test_poll_deployment.py` | Unit tests for two-stage polling + timeout paths |
| `client/package.json` | Frontend deps (React, Vite, TypeScript) |
| `client/tsconfig.json` | TypeScript config |
| `client/vite.config.ts` | Vite config with dev proxy |
| `client/index.html` | HTML entry point |
| `client/src/types.ts` | Message type |
| `client/src/useChat.ts` | Conversation state + `/responses` API calls |
| `client/src/ChatPanel.tsx` | Message list + input form |
| `client/src/App.tsx` | Root component |
| `client/src/main.tsx` | React entry point |

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `app.yml`
- Create: `tools/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "apx-builder"
version = "0.1.0"
description = "Natural language agent builder — describe your agent and I'll build and deploy it"
requires-python = ">=3.11"
dependencies = [
    "apx-agent",
    "fastapi>=0.119.0",
    "uvicorn>=0.37.0",
    "databricks-sdk>=0.74.0",
    "httpx>=0.27.0",
]

[dependency-groups]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
]

[tool.uv.sources]
apx-agent = { path = "../..", editable = true }

[tool.pytest.ini_options]
addopts = "-m 'not integration'"

[tool.apx.metadata]
app-name = "apx-builder"
app-entrypoint = "app:app"

[tool.apx.agent]
name = "apx_builder"
description = "Natural language agent builder — describe your agent and I'll build and deploy it"
model = "databricks-claude-sonnet-4-6"
url = ""

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: Create app.yml**

```yaml
command:
  - uvicorn
  - app:app
  - --workers
  - "1"
```

- [ ] **Step 3: Create empty init files**

```bash
mkdir -p tools tests
touch tools/__init__.py tests/__init__.py
```

- [ ] **Step 4: Install deps**

```bash
cd ~/Documents/apx-agent/python/examples/apx-builder
uv sync
```

Expected: deps resolved, `.venv` created.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml app.yml tools/__init__.py tests/__init__.py
git commit -m "feat(apx-builder): project scaffold"
```

---

## Task 2: discover_tables tool

**Files:**
- Create: `tools/discover_tables.py`
- Create: `tests/test_discover_tables.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_discover_tables.py`:

```python
from unittest.mock import MagicMock
from tools.discover_tables import search_tables, list_genie_spaces


def test_search_tables_returns_dot_separated_identifiers():
    sql = MagicMock(return_value=[
        {"table_catalog": "main", "table_schema": "sales", "table_name": "orders", "comment": "Order records"},
    ])
    result = search_tables("orders", sql)
    assert result == [{"identifier": "main.sales.orders", "comment": "Order records"}]


def test_search_tables_uses_bind_parameters():
    sql = MagicMock(return_value=[])
    search_tables("test", sql)
    call_args = sql.call_args
    query = call_args[0][0]
    assert ":pattern" in query
    params = call_args[1].get("parameters") or call_args[0][1]
    assert any(p["name"] == "pattern" for p in params)


def test_search_tables_empty_results():
    sql = MagicMock(return_value=[])
    assert search_tables("nonexistent", sql) == []


def test_list_genie_spaces_returns_id_and_name():
    ws = MagicMock()
    ws.api_client.do.return_value = {
        "spaces": [{"space_id": "abc123", "title": "Sales Analytics"}]
    }
    result = list_genie_spaces(ws)
    assert result == [{"id": "abc123", "name": "Sales Analytics"}]


def test_list_genie_spaces_empty_workspace():
    ws = MagicMock()
    ws.api_client.do.return_value = {}
    assert list_genie_spaces(ws) == []
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd ~/Documents/apx-agent/python/examples/apx-builder
uv run pytest tests/test_discover_tables.py -v
```

Expected: `ModuleNotFoundError: No module named 'tools.discover_tables'`

- [ ] **Step 3: Implement discover_tables.py**

Create `tools/discover_tables.py`:

```python
from apx_agent import Dependencies


def search_tables(search_term: str, sql: Dependencies.Sql) -> list[dict]:
    """Search Unity Catalog for tables matching a term. Returns catalog.schema.table identifiers."""
    rows = sql(
        "SELECT table_catalog, table_schema, table_name, comment "
        "FROM system.information_schema.tables "
        "WHERE lower(table_name) LIKE :pattern "
        "   OR lower(coalesce(comment, '')) LIKE :pattern "
        "LIMIT 20",
        parameters=[{"name": "pattern", "value": f"%{search_term.lower()}%", "type": "STRING"}],
    )
    return [
        {
            "identifier": f"{r['table_catalog']}.{r['table_schema']}.{r['table_name']}",
            "comment": r.get("comment") or "",
        }
        for r in rows
    ]


def list_genie_spaces(ws: Dependencies.UserClient) -> list[dict]:
    """List Genie spaces available in this workspace. Returns id and name for each space."""
    response = ws.api_client.do("GET", "/api/2.0/genie/spaces")
    return [
        {"id": s["space_id"], "name": s["title"]}
        for s in response.get("spaces", [])
    ]
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_discover_tables.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/discover_tables.py tests/test_discover_tables.py
git commit -m "feat(apx-builder): discover_tables and list_genie_spaces tools"
```

---

## Task 3: scaffold_project tool

**Files:**
- Create: `tools/scaffold_project.py`
- Create: `tests/test_scaffold_project.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scaffold_project.py`:

```python
from unittest.mock import MagicMock, patch, call
from tools.scaffold_project import _generate_files, scaffold_project


def test_generate_files_returns_three_required_files():
    files = _generate_files("answer sales questions", ["main.sales.orders"], [], "mcp-sales")
    assert set(files.keys()) == {"app.py", "pyproject.toml", "app.yml"}


def test_generate_files_app_py_includes_sql_tool_for_each_table():
    files = _generate_files("answer sales questions", ["main.sales.orders", "main.sales.customers"], [], "mcp-sales")
    assert "main.sales.orders" in files["app.py"]
    assert "main.sales.customers" in files["app.py"]
    assert "sql_tool" in files["app.py"]


def test_generate_files_app_py_includes_genie_tool_for_space():
    files = _generate_files("answer sales questions", [], [{"id": "abc123", "name": "Sales"}], "mcp-sales")
    assert "abc123" in files["app.py"]
    assert "genie_tool" in files["app.py"]


def test_generate_files_includes_lineage_tool_when_requested():
    files = _generate_files("explore lineage", ["main.sales.orders"], [], "mcp-lineage", include_lineage=True)
    assert "lineage_tool" in files["app.py"]


def test_generate_files_no_lineage_by_default():
    files = _generate_files("answer questions", ["main.sales.orders"], [], "mcp-agent")
    assert "lineage_tool" not in files["app.py"]


def test_generate_files_app_name_in_pyproject():
    files = _generate_files("test", ["a.b.c"], [], "mcp-test-agent")
    assert "mcp-test-agent" in files["pyproject.toml"]


def test_generate_files_use_case_in_instructions():
    files = _generate_files("handle customer refunds", ["main.billing.transactions"], [], "mcp-refunds")
    assert "handle customer refunds" in files["app.py"]


def test_scaffold_project_uploads_all_files():
    ws = MagicMock()
    ws.current_user.me.return_value = MagicMock(user_name="user@example.com")

    with patch("tools.scaffold_project._generate_files") as mock_gen, \
         patch("tools.scaffold_project._upload_files") as mock_upload:
        mock_gen.return_value = {"app.py": "code", "pyproject.toml": "toml", "app.yml": "yaml"}

        result = scaffold_project("test", ["a.b.c"], [], "mcp-test", False, ws)

    mock_upload.assert_called_once()
    upload_args = mock_upload.call_args[0]
    assert upload_args[0] is ws
    assert upload_args[1] == {"app.py": "code", "pyproject.toml": "toml", "app.yml": "yaml"}
    assert "mcp-test" in upload_args[2]
    assert "user@example.com" in upload_args[2]
    assert "mcp-test" in result
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
uv run pytest tests/test_scaffold_project.py -v
```

Expected: `ModuleNotFoundError: No module named 'tools.scaffold_project'`

- [ ] **Step 3: Implement scaffold_project.py**

Create `tools/scaffold_project.py`:

```python
import base64
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import ImportFormat
from apx_agent import Dependencies


def _generate_files(
    use_case: str,
    tables: list[str],
    genie_spaces: list[dict],
    app_name: str,
    include_lineage: bool = False,
) -> dict[str, str]:
    """Generate apx-agent project files. Returns {filename: content}. Pure function — no side effects."""
    tool_imports = []
    tool_calls = []

    if tables:
        tool_imports.append("sql_tool")
        for table in tables:
            tool_calls.append(f'    sql_tool("{table}"),')

    if genie_spaces:
        tool_imports.append("genie_tool")
        for space in genie_spaces:
            tool_calls.append(f'    genie_tool("{space["id"]}"),  # {space["name"]}')

    if include_lineage:
        tool_imports.append("lineage_tool")
        tool_calls.append("    lineage_tool(),")

    imports_str = ", ".join(tool_imports)
    tools_str = "\n".join(tool_calls)

    app_py = f'''\
from apx_agent import Agent, create_app, {imports_str}

agent = Agent(
    tools=[
{tools_str}
    ],
    instructions="You are a data assistant for: {use_case}. Answer questions using the available tools.",
)
app = create_app(agent)
'''

    pyproject_toml = f'''\
[project]
name = "{app_name}"
requires-python = ">=3.11"
dependencies = [
    "apx-agent @ git+https://github.com/stuagano/apx-agent.git",
]

[tool.apx.agent]
name = "{app_name}"
description = "{use_case}"
model = "databricks-claude-sonnet-4-6"
url = ""

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
'''

    app_yml = '''\
command:
  - uvicorn
  - app:app
  - --workers
  - "1"
'''

    return {"app.py": app_py, "pyproject.toml": pyproject_toml, "app.yml": app_yml}


def _upload_files(ws: WorkspaceClient, files: dict[str, str], workspace_path: str) -> None:
    """Upload project files to the Databricks Workspace."""
    ws.workspace.mkdirs(workspace_path)
    for filename, content in files.items():
        ws.workspace.import_(
            path=f"{workspace_path}/{filename}",
            content=base64.b64encode(content.encode()).decode(),
            format=ImportFormat.AUTO,
            overwrite=True,
        )


def scaffold_project(
    use_case: str,
    tables: list[str],
    genie_spaces: list[dict],
    app_name: str,
    include_lineage: bool,
    ws: Dependencies.UserClient,
) -> str:
    """Scaffold an apx-agent project in the Databricks Workspace. Returns the workspace path."""
    email = ws.current_user.me().user_name
    workspace_path = f"/Users/{email}/apx-builder/{app_name}"
    files = _generate_files(use_case, tables, genie_spaces, app_name, include_lineage)
    _upload_files(ws, files, workspace_path)
    return workspace_path
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_scaffold_project.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/scaffold_project.py tests/test_scaffold_project.py
git commit -m "feat(apx-builder): scaffold_project tool — generates and uploads agent files"
```

---

## Task 4: deploy_agent tool

**Files:**
- Create: `tools/deploy_agent.py`
- Create: `tests/test_deploy_agent.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deploy_agent.py`:

```python
from unittest.mock import MagicMock
from databricks.sdk.errors import NotFound
from tools.deploy_agent import deploy_agent


def test_creates_app_when_it_does_not_exist():
    ws = MagicMock()
    ws.apps.get.side_effect = NotFound("not found")

    deploy_agent("mcp-my-agent", "/Users/test/apx-builder/mcp-my-agent", ws)

    ws.apps.create.assert_called_once_with(
        name="mcp-my-agent",
        description="Agent built by apx-builder",
    )


def test_skips_create_when_app_already_exists():
    ws = MagicMock()
    ws.apps.get.return_value = MagicMock()

    deploy_agent("mcp-my-agent", "/Users/test/apx-builder/mcp-my-agent", ws)

    ws.apps.create.assert_not_called()


def test_always_calls_deploy_with_correct_path():
    ws = MagicMock()
    ws.apps.get.return_value = MagicMock()
    workspace_path = "/Users/test@example.com/apx-builder/mcp-my-agent"

    deploy_agent("mcp-my-agent", workspace_path, ws)

    ws.apps.deploy.assert_called_once()
    kwargs = ws.apps.deploy.call_args[1]
    assert kwargs["app_name"] == "mcp-my-agent"
    assert kwargs["source_code_path"] == workspace_path


def test_returns_app_name():
    ws = MagicMock()
    ws.apps.get.return_value = MagicMock()

    result = deploy_agent("mcp-my-agent", "/some/path", ws)

    assert result == "mcp-my-agent"
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
uv run pytest tests/test_deploy_agent.py -v
```

Expected: `ModuleNotFoundError: No module named 'tools.deploy_agent'`

- [ ] **Step 3: Implement deploy_agent.py**

Create `tools/deploy_agent.py`:

```python
from databricks.sdk.errors import NotFound
from apx_agent import Dependencies


def deploy_agent(app_name: str, workspace_path: str, ws: Dependencies.UserClient) -> str:
    """Create (if needed) and deploy an apx-agent project as a Databricks App. Returns app_name."""
    try:
        ws.apps.get(app_name)
    except NotFound:
        ws.apps.create(name=app_name, description="Agent built by apx-builder")

    ws.apps.deploy(app_name=app_name, source_code_path=workspace_path)
    return app_name
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_deploy_agent.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/deploy_agent.py tests/test_deploy_agent.py
git commit -m "feat(apx-builder): deploy_agent tool — create-if-missing + deploy"
```

---

## Task 5: poll_deployment tool

**Files:**
- Create: `tools/poll_deployment.py`
- Create: `tests/test_poll_deployment.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_poll_deployment.py`:

```python
import pytest
from unittest.mock import MagicMock, patch
from tools.poll_deployment import poll_deployment


def _make_app(api_state="RUNNING", deploy_state="SUCCEEDED", url="https://mcp-test.databricksapps.com"):
    app = MagicMock()
    app.app_status.state.value = api_state
    app.active_deployment.status.state.value = deploy_state
    app.url = url
    return app


def test_returns_url_after_both_stages_pass():
    ws = MagicMock()
    ws.apps.get.return_value = _make_app()

    with patch("httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200)
        result = poll_deployment("mcp-test", ws)

    assert result == "https://mcp-test.databricksapps.com"


def test_stage1_retries_until_running():
    ws = MagicMock()
    ws.apps.get.side_effect = [
        _make_app(api_state="DEPLOYING", deploy_state="IN_PROGRESS"),
        _make_app(api_state="RUNNING", deploy_state="SUCCEEDED"),
    ]

    with patch("httpx.get") as mock_get, patch("time.sleep"):
        mock_get.return_value = MagicMock(status_code=200)
        result = poll_deployment("mcp-test", ws)

    assert ws.apps.get.call_count == 2
    assert result == "https://mcp-test.databricksapps.com"


def test_stage2_retries_until_health_200():
    ws = MagicMock()
    ws.apps.get.return_value = _make_app()

    with patch("httpx.get") as mock_get, patch("time.sleep"):
        mock_get.side_effect = [
            Exception("connection refused"),
            MagicMock(status_code=200),
        ]
        result = poll_deployment("mcp-test", ws)

    assert mock_get.call_count == 2
    assert result == "https://mcp-test.databricksapps.com"


def test_stage2_timeout_returns_warning_url():
    ws = MagicMock()
    ws.apps.get.return_value = _make_app()

    # time.time returns: start(0), stage1 check(1), stage2 start(2), then always past deadline
    time_values = [0, 1, 2] + [200] * 20

    with patch("httpx.get", side_effect=Exception("down")), \
         patch("time.sleep"), \
         patch("time.time", side_effect=time_values):
        result = poll_deployment("mcp-test", ws)

    assert "https://mcp-test.databricksapps.com" in result
    assert "30 seconds" in result


def test_stage1_timeout_raises():
    ws = MagicMock()
    ws.apps.get.return_value = _make_app(api_state="DEPLOYING", deploy_state="IN_PROGRESS")

    # time.time: start(0), then immediately past 120s deadline
    time_values = [0] + [200] * 5

    with patch("time.sleep"), patch("time.time", side_effect=time_values):
        with pytest.raises(TimeoutError, match="RUNNING"):
            poll_deployment("mcp-test", ws)
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
uv run pytest tests/test_poll_deployment.py -v
```

Expected: `ModuleNotFoundError: No module named 'tools.poll_deployment'`

- [ ] **Step 3: Implement poll_deployment.py**

Create `tools/poll_deployment.py`:

```python
import time
import httpx
from apx_agent import Dependencies


def poll_deployment(app_name: str, ws: Dependencies.UserClient) -> str:
    """Wait for agent to be fully live. Returns URL when ready, or URL with warning on Stage 2 timeout."""
    # Stage 1: API readiness (up to 120s)
    deadline = time.time() + 120
    app = None
    while time.time() < deadline:
        app = ws.apps.get(app_name)
        api_state = app.app_status.state.value if app.app_status else ""
        deploy_state = (
            app.active_deployment.status.state.value
            if app.active_deployment and app.active_deployment.status
            else ""
        )
        if api_state == "RUNNING" and deploy_state == "SUCCEEDED":
            break
        time.sleep(5)
    else:
        raise TimeoutError(f"App '{app_name}' did not reach RUNNING state within 120s")

    app_url = app.url

    # Stage 2: HTTP readiness (up to 60s)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            r = httpx.get(f"{app_url}/health", timeout=5.0)
            if r.status_code == 200:
                return app_url
        except Exception:
            pass
        time.sleep(5)

    return f"{app_url} (warning: health check timed out — try in 30 seconds)"
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_poll_deployment.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: 22 passed, 0 failed.

- [ ] **Step 6: Commit**

```bash
git add tools/poll_deployment.py tests/test_poll_deployment.py
git commit -m "feat(apx-builder): poll_deployment tool — two-stage API + HTTP health check"
```

---

## Task 6: system_prompt

**Files:**
- Create: `system_prompt.py`

- [ ] **Step 1: Create system_prompt.py**

```python
SYSTEM_PROMPT = """\
You are the apx-builder — a conversational assistant that helps Databricks field reps
build and deploy data agents for their customers.

## Your job

Run a short discovery conversation (3–5 questions max), then autonomously scaffold and
deploy the agent. The rep ends the conversation with a live URL they can hand to their
customer. No coding required on their part.

## Discovery flow

Ask ONE question at a time. Never ask more than one. Adapt based on answers.

1. **Use case** — Ask: "What should this agent do? Describe it in plain English."
   Listen for the domain and task (e.g., "answer questions about our sales data").

2. **Data sources** — Call `search_tables` with 2–3 keywords from their description.
   Present the matching tables as options. If they mention Genie or an AI assistant,
   call `list_genie_spaces` and present the results. Let them pick. Follow up if needed.

3. **Lineage** — If they asked about data discovery, column origins, or data lineage,
   set `include_lineage=True` when calling `scaffold_project`. Otherwise omit it.

4. **Name** — Suggest a slug: lowercase, hyphens, prefixed with `mcp-`
   (e.g., "mcp-sales-agent"). Ask: "What should we call it? I'd suggest: mcp-{suggestion}."

Once you have (1) use case, (2) tables/spaces, (3) app name — move to the build phase.
Do not ask additional questions once you have these three things.

## Build phase

Announce: "Got everything I need — building your agent now."

Then call the tools IN ORDER — do not skip steps:
1. `scaffold_project` — uploads project files to workspace
2. `deploy_agent` — creates and deploys the Databricks App
3. `poll_deployment` — waits for the app to be live (BOTH API state + HTTP health)

## Finishing

When `poll_deployment` returns a URL without "warning":
"Your agent is live at [URL]. It can answer questions about [describe what it knows].
Try asking it: [write a concrete, realistic example question based on what the rep told you]."

When `poll_deployment` returns a URL with "warning":
"Your agent deployed successfully. It's still starting up — [URL] should be ready in about 30 seconds."

## Rules

- Never share the URL before `poll_deployment` confirms it is live.
- Never ask two questions in the same message.
- Keep language simple — no technical jargon. The rep is not a developer.
- If `scaffold_project` or `deploy_agent` fails, explain what happened in plain language
  and ask if they want to try again.
"""
```

- [ ] **Step 2: Commit**

```bash
git add system_prompt.py
git commit -m "feat(apx-builder): system prompt — discovery flow + build phase instructions"
```

---

## Task 7: Main app.py

**Files:**
- Create: `app.py`

- [ ] **Step 1: Create app.py**

```python
from pathlib import Path

from apx_agent import Agent, create_app
from fastapi.responses import FileResponse

from system_prompt import SYSTEM_PROMPT
from tools.discover_tables import list_genie_spaces, search_tables
from tools.deploy_agent import deploy_agent
from tools.poll_deployment import poll_deployment
from tools.scaffold_project import scaffold_project

agent = Agent(
    tools=[search_tables, list_genie_spaces, scaffold_project, deploy_agent, poll_deployment],
    instructions=SYSTEM_PROMPT,
)
app = create_app(agent)

# Serve the React frontend via explicit GET routes.
# DO NOT use app.mount("/", StaticFiles(...)) — it intercepts POST /responses.
_here = Path(__file__).resolve()
_candidates = [
    Path.cwd() / "client" / "dist",
    _here.parent / "client" / "dist",
]
_CLIENT_DIST = next((c for c in _candidates if c.exists()), None)

if _CLIENT_DIST is not None:
    @app.get("/", include_in_schema=False)
    def spa_index():
        return FileResponse(str(_CLIENT_DIST / "index.html"))

    @app.get("/assets/{path:path}", include_in_schema=False)
    def spa_assets(path: str):
        asset = _CLIENT_DIST / "assets" / path
        return FileResponse(str(asset) if asset.is_file() else str(_CLIENT_DIST / "index.html"))
```

- [ ] **Step 2: Verify the app starts**

```bash
cd ~/Documents/apx-agent/python/examples/apx-builder
DATABRICKS_CONFIG_PROFILE=<your-profile> uv run uvicorn app:app --reload --port 8000
```

Expected output contains: `Application startup complete.` and no import errors.

- [ ] **Step 3: Verify agent card**

```bash
curl http://localhost:8000/.well-known/agent.json | python3 -m json.tool
```

Expected: JSON with `"tools"` array containing `search_tables`, `list_genie_spaces`, `scaffold_project`, `deploy_agent`, `poll_deployment`.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat(apx-builder): main app — wire agent tools + frontend serving"
```

---

## Task 8: React frontend

**Files:**
- Create: `client/package.json`
- Create: `client/tsconfig.json`
- Create: `client/vite.config.ts`
- Create: `client/index.html`
- Create: `client/src/types.ts`
- Create: `client/src/useChat.ts`
- Create: `client/src/ChatPanel.tsx`
- Create: `client/src/App.tsx`
- Create: `client/src/main.tsx`

- [ ] **Step 1: Create client/package.json**

```json
{
  "name": "apx-builder-client",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@types/react": "^18.3.1",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.4",
    "typescript": "^5.6.3",
    "vite": "^6.0.3"
  }
}
```

- [ ] **Step 2: Create client/tsconfig.json**

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true
  },
  "include": ["src"]
}
```

- [ ] **Step 3: Create client/vite.config.ts**

```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/responses': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
```

- [ ] **Step 4: Create client/index.html**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Agent Builder</title>
    <style>body { margin: 0; font-family: system-ui, -apple-system, sans-serif; }</style>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 5: Create client/src/types.ts**

```typescript
export interface Message {
  role: 'user' | 'assistant'
  content: string
}
```

- [ ] **Step 6: Create client/src/useChat.ts**

```typescript
import { useState, useCallback } from 'react'
import type { Message } from './types'

export function useChat() {
  const [messages, setMessages] = useState<Message[]>([])
  const [isLoading, setIsLoading] = useState(false)

  const sendMessage = useCallback(async (text: string) => {
    const userMsg: Message = { role: 'user', content: text }
    const next = [...messages, userMsg]
    setMessages(next)
    setIsLoading(true)
    try {
      const resp = await fetch('/responses', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input: next.map(m => ({ role: m.role, content: m.content })),
        }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      const outputText: string =
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        data?.output?.find((o: any) => o.type === 'message')
          ?.content?.[0]?.text ?? 'No response.'
      setMessages(prev => [...prev, { role: 'assistant', content: outputText }])
    } catch {
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: 'Something went wrong. Please try again.' },
      ])
    } finally {
      setIsLoading(false)
    }
  }, [messages])

  const reset = useCallback(() => setMessages([]), [])

  return { messages, isLoading, sendMessage, reset }
}
```

- [ ] **Step 7: Create client/src/ChatPanel.tsx**

```typescript
import { useState, useRef, useEffect } from 'react'
import type { Message } from './types'

interface Props {
  messages: Message[]
  isLoading: boolean
  onSend: (text: string) => void
  onReset: () => void
}

export function ChatPanel({ messages, isLoading, onSend, onReset }: Props) {
  const [input, setInput] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || isLoading) return
    setInput('')
    onSend(text)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', maxWidth: 720, margin: '0 auto', padding: '0 16px' }}>
      <div style={{ padding: '16px 0', borderBottom: '1px solid #eee', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Agent Builder</h1>
        {messages.length > 0 && (
          <button onClick={onReset} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#888', fontSize: 13 }}>
            New agent
          </button>
        )}
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '24px 0' }}>
        {messages.length === 0 && (
          <p style={{ color: '#888', textAlign: 'center', marginTop: 80 }}>
            Tell me what you want your agent to do.
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{ marginBottom: 16, textAlign: m.role === 'user' ? 'right' : 'left' }}>
            <span style={{
              display: 'inline-block',
              padding: '8px 14px',
              borderRadius: 12,
              background: m.role === 'user' ? '#0066ff' : '#f0f0f0',
              color: m.role === 'user' ? '#fff' : '#000',
              maxWidth: '80%',
              whiteSpace: 'pre-wrap',
              fontSize: 14,
              lineHeight: 1.5,
            }}>
              {m.content}
            </span>
          </div>
        ))}
        {isLoading && (
          <div style={{ color: '#888', fontSize: 13, padding: '4px 0' }}>Thinking...</div>
        )}
        <div ref={bottomRef} />
      </div>

      <form onSubmit={handleSubmit} style={{ padding: '16px 0', borderTop: '1px solid #eee', display: 'flex', gap: 8 }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Describe your agent..."
          disabled={isLoading}
          autoFocus
          style={{ flex: 1, padding: '8px 12px', borderRadius: 8, border: '1px solid #ddd', fontSize: 14 }}
        />
        <button
          type="submit"
          disabled={isLoading || !input.trim()}
          style={{ padding: '8px 16px', background: '#0066ff', color: '#fff', border: 'none', borderRadius: 8, cursor: 'pointer', fontSize: 14 }}
        >
          Send
        </button>
      </form>
    </div>
  )
}
```

- [ ] **Step 8: Create client/src/App.tsx**

```typescript
import { useChat } from './useChat'
import { ChatPanel } from './ChatPanel'

export default function App() {
  const { messages, isLoading, sendMessage, reset } = useChat()
  return <ChatPanel messages={messages} isLoading={isLoading} onSend={sendMessage} onReset={reset} />
}
```

- [ ] **Step 9: Create client/src/main.tsx**

```typescript
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
```

- [ ] **Step 10: Install frontend deps**

```bash
cd ~/Documents/apx-agent/python/examples/apx-builder/client
npm install
```

Expected: `node_modules/` created, no errors.

- [ ] **Step 11: Verify dev build works**

```bash
npm run build
```

Expected: `dist/` created, `dist/index.html` present.

- [ ] **Step 12: Commit**

```bash
cd ~/Documents/apx-agent/python/examples/apx-builder
git add client/
git commit -m "feat(apx-builder): React chat frontend"
```

---

## Task 9: Local smoke test + deploy

**Files:** none (verification + deployment only)

- [ ] **Step 1: Run all Python tests one final time**

```bash
cd ~/Documents/apx-agent/python/examples/apx-builder
uv run pytest tests/ -v
```

Expected: 18 passed, 0 failed.

- [ ] **Step 2: Build the frontend**

```bash
cd client && npm run build && cd ..
```

Expected: `client/dist/index.html` present.

- [ ] **Step 3: Start backend locally**

```bash
DATABRICKS_CONFIG_PROFILE=<your-profile> uv run uvicorn app:app --reload --port 8000
```

- [ ] **Step 4: Verify the UI loads**

Open `http://localhost:8000` in a browser. Expected: chat interface with "Tell me what you want your agent to do."

- [ ] **Step 5: Smoke test the conversation**

Type: "I want an agent that answers questions about our sales data"

Expected: agent responds with a follow-up question about tables (and likely calls `search_tables` in the background).

Stop the backend (`Ctrl+C`) when satisfied.

- [ ] **Step 6: Deploy apx-builder to your workspace**

```bash
databricks apps deploy apx-builder --source-code-path . --profile <your-profile>
```

- [ ] **Step 7: Verify the deployment is live (two-stage check)**

```bash
# Stage 1: API state
databricks apps get apx-builder --profile <your-profile> -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('App state:', d.get('app_status', {}).get('state'))
print('Deploy state:', d.get('active_deployment', {}).get('status', {}).get('state'))
print('URL:', d.get('url'))
"

# Stage 2: HTTP health (only after Stage 1 shows RUNNING + SUCCEEDED)
curl https://<your-app-url>/health
```

Expected Stage 1: `App state: RUNNING`, `Deploy state: SUCCEEDED`
Expected Stage 2: HTTP 200

- [ ] **Step 8: Verify the chat UI is live**

Open `https://<your-app-url>` in a browser. Expected: chat interface loads, agent responds to a message.

- [ ] **Step 9: Final commit**

```bash
git commit --allow-empty -m "chore(apx-builder): deployed to <workspace-name>"
```
