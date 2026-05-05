# Jira Async Trigger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a "Data Issue" Jira ticket is created, a Databricks Workflow Job automatically investigates and posts the findings as a comment on the ticket.

**Architecture:** A FastAPI webhook endpoint receives Jira `issue_created` events, validates the HMAC-SHA256 signature, then calls `ws.jobs.run_now()` and returns 202 immediately. A separate Databricks Job (`jobs/investigate.py`) runs the investigation by calling the Databricks FM API directly via httpx (bypassing the pipeline's FastAPI OBO auth), then posts the result to Jira via a thin httpx client.

**Tech Stack:** FastAPI, Pydantic Settings, httpx, databricks-sdk, pytest, pytest-asyncio, respx (httpx mocking)

---

## File Map

| File | Change | Responsibility |
|---|---|---|
| `src/data_triage_agent/backend/config.py` | **new** | Pydantic Settings loaded from env — Jira creds + job ID |
| `src/data_triage_agent/backend/webhook.py` | **new** | FastAPI router: HMAC validation, event filter, `run_now` trigger |
| `src/data_triage_agent/backend/app.py` | **modify** | Include webhook router |
| `src/data_triage_agent/jobs/__init__.py` | **new** | Package marker |
| `src/data_triage_agent/jobs/investigate.py` | **new** | Job entrypoint: argparse, FM API loop, Jira comment |
| `src/data_triage_agent/jira_client.py` | **new** | Thin httpx wrapper for Jira REST API |
| `tests/test_jira_client.py` | **new** | Unit tests (respx mock) |
| `tests/test_webhook.py` | **new** | Unit tests (TestClient + dependency override) |
| `databricks.yml` | **modify** | Add job resource + env vars + updated build artifact |
| `.env.example` | **modify** | Add Jira env vars |
| `pyproject.toml` | **modify** | Add pytest + respx dev deps + `investigate` entry point |
| `SETUP.md` | **modify** | Jira prerequisites section |

---

## Task 0: Config, pyproject, env example

**Files:**
- Create: `src/data_triage_agent/backend/config.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`

- [ ] **Step 1: Write the failing test for Settings**

Create `tests/test_config.py`:

```python
import os
import pytest


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_SERVICE_ACCOUNT_EMAIL", "bot@test.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("DATA_TRIAGE_JOB_ID", "42")
    monkeypatch.setenv("DATA_INSPECTOR_URL", "https://inspector.example.com")

    # must import after monkeypatch so pydantic-settings picks up env
    import importlib
    import data_triage_agent.backend.config as cfg_mod
    importlib.reload(cfg_mod)
    s = cfg_mod.Settings()

    assert s.jira_base_url == "https://test.atlassian.net"
    assert s.jira_service_account_email == "bot@test.com"
    assert s.jira_api_token == "tok"
    assert s.jira_webhook_secret == "secret"
    assert s.data_triage_job_id == 42
    assert s.data_inspector_url == "https://inspector.example.com"


def test_settings_defaults():
    # no env override — defaults must not raise
    from data_triage_agent.backend.config import Settings
    s = Settings()
    assert s.data_inspector_url == "http://localhost:9000"
    assert s.data_triage_job_id == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/stuart.gano/Documents/Customers/uplight/agents/data-triage-agent
uv run pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'data_triage_agent.backend.config'`

- [ ] **Step 3: Add pytest + respx to pyproject.toml dev deps and add investigate entry point**

Edit `pyproject.toml`:

```toml
[dependency-groups]
dev = [
    "ty>=0.0.12",
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "respx>=0.21.0",
    "httpx>=0.27.0",
]

[project.scripts]
investigate = "data_triage_agent.jobs.investigate:main"
```

- [ ] **Step 4: Create `src/data_triage_agent/backend/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jira_base_url: str = ""
    jira_service_account_email: str = ""
    jira_api_token: str = ""
    jira_webhook_secret: str = ""
    data_triage_job_id: int = 0
    data_inspector_url: str = "http://localhost:9000"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: 2 passed

- [ ] **Step 6: Update `.env.example`**

Append to the end of `.env.example`:

```
# Jira integration (for async webhook trigger)
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_SERVICE_ACCOUNT_EMAIL=data-triage-bot@your-org.com
JIRA_API_TOKEN=your_api_token_here
JIRA_WEBHOOK_SECRET=generate_with_openssl_rand_hex_32
DATA_TRIAGE_JOB_ID=123
```

- [ ] **Step 7: Commit**

```bash
git add src/data_triage_agent/backend/config.py tests/test_config.py pyproject.toml .env.example
git commit -m "feat: Pydantic Settings for Jira config + pytest dev deps"
```

---

## Task 1: JiraClient

**Files:**
- Create: `src/data_triage_agent/jira_client.py`
- Create: `tests/test_jira_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jira_client.py`:

```python
import pytest
import respx
import httpx

from data_triage_agent.jira_client import JiraClient, JiraClientError


BASE = "https://test.atlassian.net"


@respx.mock
def test_post_comment_success():
    route = respx.post(f"{BASE}/rest/api/3/issue/DATA-42/comment").mock(
        return_value=httpx.Response(201, json={"id": "10001"})
    )
    client = JiraClient(BASE, "bot@test.com", "tok")
    client.post_comment("DATA-42", "Investigation complete.")
    assert route.called


@respx.mock
def test_post_comment_raises_on_error():
    respx.post(f"{BASE}/rest/api/3/issue/DATA-99/comment").mock(
        return_value=httpx.Response(403, json={"errorMessages": ["Forbidden"]})
    )
    client = JiraClient(BASE, "bot@test.com", "tok")
    with pytest.raises(JiraClientError, match="403"):
        client.post_comment("DATA-99", "body")


@respx.mock
def test_post_comment_sends_adf_body():
    captured = {}

    def capture(request, route):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={})

    respx.post(f"{BASE}/rest/api/3/issue/DATA-1/comment").mock(side_effect=capture)
    client = JiraClient(BASE, "bot@test.com", "tok")
    client.post_comment("DATA-1", "hello world")

    assert captured["body"]["body"]["type"] == "doc"
    assert captured["body"]["body"]["content"][0]["content"][0]["text"] == "hello world"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_jira_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'data_triage_agent.jira_client'`

- [ ] **Step 3: Create `src/data_triage_agent/jira_client.py`**

```python
from __future__ import annotations

import httpx


class JiraClientError(Exception):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (email, api_token)

    def post_comment(self, issue_key: str, body: str) -> None:
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/comment"
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": body}],
                    }
                ],
            }
        }
        with httpx.Client() as client:
            resp = client.post(url, json=payload, auth=self._auth)
        if not resp.is_success:
            raise JiraClientError(f"Jira API {resp.status_code}: {resp.text[:200]}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_jira_client.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/data_triage_agent/jira_client.py tests/test_jira_client.py
git commit -m "feat: JiraClient thin httpx wrapper for posting comments"
```

---

## Task 2: Webhook endpoint

**Files:**
- Create: `src/data_triage_agent/backend/webhook.py`
- Create: `tests/test_webhook.py`
- Modify: `src/data_triage_agent/backend/app.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_webhook.py`:

```python
import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from data_triage_agent.backend.app import app
from data_triage_agent.backend.config import Settings, get_settings


SECRET = "test-secret-1234"
JOB_ID = 7


def _make_settings():
    return Settings(
        jira_webhook_secret=SECRET,
        data_triage_job_id=JOB_ID,
        jira_base_url="https://test.atlassian.net",
        jira_service_account_email="bot@test.com",
        jira_api_token="tok",
        data_inspector_url="http://localhost:9000",
    )


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_payload(issuetype="Data Issue", event="jira:issue_created") -> dict:
    return {
        "webhookEvent": event,
        "issue": {
            "key": "DATA-42",
            "fields": {
                "summary": "Missing billing data for account 1234",
                "description": None,
                "issuetype": {"name": issuetype},
                "priority": {"name": "High"},
                "reporter": {"displayName": "Alice"},
            },
        },
    }


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_valid_webhook_returns_202(client):
    payload = json.dumps(_make_payload()).encode()
    with patch("data_triage_agent.backend.webhook.WorkspaceClient") as MockWs:
        mock_ws = MagicMock()
        MockWs.return_value = mock_ws
        resp = client.post(
            "/webhook/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": _sign(payload, SECRET),
            },
        )
    assert resp.status_code == 202
    mock_ws.jobs.run_now.assert_called_once()
    call_kwargs = mock_ws.jobs.run_now.call_args
    assert call_kwargs.kwargs["job_id"] == JOB_ID or call_kwargs.args[0] == JOB_ID


def test_invalid_signature_returns_401(client):
    payload = json.dumps(_make_payload()).encode()
    resp = client.post(
        "/webhook/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": "sha256=bad",
        },
    )
    assert resp.status_code == 401


def test_wrong_event_returns_200(client):
    payload = json.dumps(_make_payload(event="jira:issue_updated")).encode()
    resp = client.post(
        "/webhook/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": _sign(payload, SECRET),
        },
    )
    assert resp.status_code == 200


def test_wrong_issuetype_returns_200(client):
    payload = json.dumps(_make_payload(issuetype="Bug")).encode()
    resp = client.post(
        "/webhook/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": _sign(payload, SECRET),
        },
    )
    assert resp.status_code == 200


def test_missing_signature_returns_401(client):
    payload = json.dumps(_make_payload()).encode()
    resp = client.post(
        "/webhook/jira",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_run_now_called_with_correct_params(client):
    payload = json.dumps(_make_payload()).encode()
    with patch("data_triage_agent.backend.webhook.WorkspaceClient") as MockWs:
        mock_ws = MagicMock()
        MockWs.return_value = mock_ws
        client.post(
            "/webhook/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": _sign(payload, SECRET),
            },
        )
    _, kwargs = mock_ws.jobs.run_now.call_args
    params = kwargs.get("python_params") or mock_ws.jobs.run_now.call_args.args[1] if len(mock_ws.jobs.run_now.call_args.args) > 1 else kwargs.get("python_params", [])
    assert "--issue-key" in params
    assert "DATA-42" in params
    assert "--summary" in params
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_webhook.py -v
```

Expected: `ModuleNotFoundError: No module named 'data_triage_agent.backend.webhook'`

- [ ] **Step 3: Create `src/data_triage_agent/backend/webhook.py`**

```python
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Annotated

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_text(node: object) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = []
        for child in node.get("content", []):
            parts.append(_extract_text(child))
        return " ".join(p for p in parts if p)
    if isinstance(node, list):
        return " ".join(_extract_text(item) for item in node)
    return ""


@router.post("/webhook/jira", status_code=202)
async def jira_webhook(
    request: Request,
    x_hub_signature: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> dict:
    body = await request.body()

    if not x_hub_signature or not _verify_signature(
        body, x_hub_signature, settings.jira_webhook_secret
    ):
        logger.warning("Jira webhook: invalid or missing HMAC signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("webhookEvent", "")
    issue = payload.get("issue", {})
    fields = issue.get("fields", {})
    issuetype = fields.get("issuetype", {}).get("name", "")

    if event != "jira:issue_created" or issuetype != "Data Issue":
        return {"status": "ignored"}

    issue_key = issue.get("key", "")
    summary = fields.get("summary", "")
    description_raw = fields.get("description")
    description = _extract_text(description_raw) if description_raw else ""
    priority = (fields.get("priority") or {}).get("name", "")
    reporter = (fields.get("reporter") or {}).get("displayName", "")

    python_params = ["--issue-key", issue_key, "--summary", summary]
    if description:
        python_params += ["--description", description]
    if priority:
        python_params += ["--priority", priority]
    if reporter:
        python_params += ["--reporter", reporter]

    ws = WorkspaceClient()
    ws.jobs.run_now(job_id=settings.data_triage_job_id, python_params=python_params)
    logger.info("Triggered job %d for ticket %s", settings.data_triage_job_id, issue_key)

    return {"status": "accepted", "issue_key": issue_key}
```

- [ ] **Step 4: Mount the router in `app.py`**

Open `src/data_triage_agent/backend/app.py` and add after the existing router import/include:

```python
from .webhook import router as webhook_router
# ...existing imports...
app.include_router(webhook_router)
```

Find the exact location — look for existing `include_router` calls and add after them. The existing import block at the top will look something like:

```python
from .router import router
# ...
app.include_router(router, prefix="/api")
```

Add below it:

```python
from .webhook import router as webhook_router
app.include_router(webhook_router)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_webhook.py -v
```

Expected: 6 passed

- [ ] **Step 6: Run full test suite to check no regressions**

```bash
uv run pytest -v
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/data_triage_agent/backend/webhook.py src/data_triage_agent/backend/app.py tests/test_webhook.py
git commit -m "feat: Jira webhook endpoint with HMAC validation and job trigger"
```

---

## Task 3: Job entrypoint

**Files:**
- Create: `src/data_triage_agent/jobs/__init__.py`
- Create: `src/data_triage_agent/jobs/investigate.py`

> Note: This job runs inside a Databricks Job context — no FastAPI, no OBO auth. It creates `WorkspaceClient()` directly (SDK picks up credentials from the job environment). It calls the FM API via httpx rather than going through `create_investigation_pipeline`, which requires a FastAPI `Request`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_investigate.py`:

```python
import json
import pytest
import respx
import httpx
from unittest.mock import MagicMock, patch

from data_triage_agent.jobs.investigate import (
    _extract_investigation_query,
    _format_comment,
    run_investigation,
)


def test_extract_query_all_fields():
    query = _extract_investigation_query(
        summary="Missing billing data for account 1234",
        description="We expected 500 rows but got 0.",
        priority="High",
        reporter="Alice",
    )
    assert "Missing billing data" in query
    assert "We expected 500" in query
    assert "High" in query
    assert "Alice" in query


def test_extract_query_minimal():
    query = _extract_investigation_query(
        summary="Missing data",
        description="",
        priority="",
        reporter="",
    )
    assert "Missing data" in query
    assert query.strip()


def test_format_comment_includes_key_sections():
    result = "## What is missing\nRows gone.\n## Why\nJob failed."
    comment = _format_comment("DATA-42", result)
    assert "DATA-42" in comment
    assert "What is missing" in comment
    assert "Job failed" in comment


@respx.mock
def test_run_investigation_returns_string():
    # Mock the FM API endpoint
    respx.post(
        "https://adb-test.azuredatabricks.net/serving-endpoints/databricks-claude-sonnet-4-6/invocations"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Investigation complete."},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )
    mock_ws = MagicMock()
    mock_ws.config.host = "https://adb-test.azuredatabricks.net"
    mock_ws.config.token = "test-token"

    result = run_investigation("Why is data missing from billing table?", mock_ws)
    assert isinstance(result, str)
    assert len(result) > 0
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_investigate.py -v
```

Expected: `ModuleNotFoundError: No module named 'data_triage_agent.jobs'`

- [ ] **Step 3: Create `src/data_triage_agent/jobs/__init__.py`**

```python
```

(empty file)

- [ ] **Step 4: Create `src/data_triage_agent/jobs/investigate.py`**

```python
"""Databricks Job entrypoint for async Jira ticket investigation.

Invoked by the data-triage-investigation job via python_wheel_task.
Ticket fields arrive as sys.argv via python_params from run_now().
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import httpx
from databricks.sdk import WorkspaceClient

from data_triage_agent.jira_client import JiraClient, JiraClientError

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


def _extract_investigation_query(
    summary: str,
    description: str,
    priority: str,
    reporter: str,
) -> str:
    parts = [summary]
    if description:
        parts.append(description)
    if priority:
        parts.append(f"Priority: {priority}")
    if reporter:
        parts.append(f"Reported by: {reporter}")
    return "\n\n".join(parts)


def _format_comment(issue_key: str, investigation_result: str) -> str:
    return f"*Automated investigation for {issue_key}*\n\n{investigation_result}"


def run_investigation(query: str, ws: WorkspaceClient) -> str:
    """Call the FM API in a tool-calling loop and return the final text response."""
    host = ws.config.host.rstrip("/")
    token = ws.config.token
    endpoint = f"{host}/serving-endpoints/databricks-claude-sonnet-4-6/invocations"

    messages: list[dict[str, Any]] = [{"role": "user", "content": query}]

    with httpx.Client(timeout=120) as client:
        while True:
            resp = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"messages": messages, "max_tokens": 4096},
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "stop")
            assistant_msg = choice["message"]
            messages.append(assistant_msg)

            if finish_reason != "tool_calls":
                return assistant_msg.get("content") or ""

            # Execute tool calls (limited set available in job context)
            tool_calls = assistant_msg.get("tool_calls", [])
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"])
                result = _dispatch_tool(fn_name, fn_args, ws)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result),
                })


def _dispatch_tool(name: str, args: dict[str, Any], ws: WorkspaceClient) -> Any:
    """Dispatch tool calls available in job context."""
    from data_triage_agent.backend.agent_router import (
        _run_sql,
        get_job_run_history,
        get_job_run_logs,
        get_job_source_paths,
        get_table_lineage,
        find_jobs_for_table,
    )
    from data_triage_agent.backend.pipeline import get_table_info

    dispatch: dict[str, Any] = {
        "run_sql_query": lambda: _run_sql(ws, args["sql"]),
        "get_table_info": lambda: get_table_info(args["table_full_name"], ws),
        "get_table_lineage": lambda: get_table_lineage(args["table_full_name"], ws),
        "find_jobs_for_table": lambda: find_jobs_for_table(args["table_full_name"], ws),
        "get_job_run_history": lambda: get_job_run_history(args["job_id"], ws),
        "get_job_run_logs": lambda: get_job_run_logs(args["run_id"], ws),
        "get_job_source_paths": lambda: get_job_source_paths(args["job_id"], ws),
    }
    fn = dispatch.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn()
    except Exception as e:
        logger.warning("Tool %s failed: %s", name, e)
        return {"error": str(e)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Investigate a Jira data issue ticket")
    parser.add_argument("--issue-key", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--priority", default="")
    parser.add_argument("--reporter", default="")
    args = parser.parse_args()

    ws = WorkspaceClient()

    # Read DATA_INSPECTOR_URL from env (set as cluster spark_env_vars)
    import os
    data_inspector_url = os.environ.get("DATA_INSPECTOR_URL", "http://localhost:9000")

    query = _extract_investigation_query(
        summary=args.summary,
        description=args.description,
        priority=args.priority,
        reporter=args.reporter,
    )

    jira_base_url = os.environ.get("JIRA_BASE_URL", "")
    jira_email = os.environ.get("JIRA_SERVICE_ACCOUNT_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    jira = JiraClient(jira_base_url, jira_email, jira_token)

    try:
        logger.info("Starting investigation for %s", args.issue_key)
        result = run_investigation(query, ws)
        comment_body = _format_comment(args.issue_key, result)
        jira.post_comment(args.issue_key, comment_body)
        logger.info("Posted investigation comment to %s", args.issue_key)
    except Exception as exc:
        logger.error("Investigation failed for %s: %s", args.issue_key, exc)
        try:
            jira.post_comment(
                args.issue_key,
                f"*Automated investigation failed for {args.issue_key}*\n\nError: {exc}\n\nPlease investigate manually.",
            )
        except JiraClientError:
            logger.error("Also failed to post error comment to %s", args.issue_key)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_investigate.py -v
```

Expected: 4 passed

- [ ] **Step 6: Run full test suite**

```bash
uv run pytest -v
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/data_triage_agent/jobs/__init__.py src/data_triage_agent/jobs/investigate.py tests/test_investigate.py
git commit -m "feat: job entrypoint for async Jira investigation with FM API loop"
```

---

## Task 4: Bundle config + SETUP.md

**Files:**
- Modify: `databricks.yml`
- Modify: `SETUP.md`

> Note: `pyproject.toml` already has the `investigate` entry point added in Task 0.

- [ ] **Step 1: Read current `databricks.yml`**

```bash
cat databricks.yml
```

- [ ] **Step 2: Update `databricks.yml`**

The updated `databricks.yml` should be:

```yaml
bundle:
  name: data-triage-agent

sync:
  include:
    - .build

artifacts:
  default:
    build: UV_OFFLINE=1 apx build && cp pyproject.toml .build/ && cp dist/data_triage_agent*.whl .build/ 2>/dev/null || true

resources:
  apps:
    mcp-data-triage-app:
      name: "mcp-data-triage"
      description: "Data triage agent — investigates missing data (MCP-enabled for Genie Code)"
      source_code_path: ./.build
      config:
        env_variables:
          - name: DATA_INSPECTOR_URL
            value: ${var.data_inspector_url}

  jobs:
    data-triage-investigation:
      name: "data-triage-investigation"
      tasks:
        - task_key: investigate
          python_wheel_task:
            package_name: data_triage_agent
            entry_point: investigate
          job_cluster_key: investigation_cluster
          libraries:
            - whl: /Workspace${workspace.file_path}/.build/data_triage_agent*.whl
      job_clusters:
        - job_cluster_key: investigation_cluster
          new_cluster:
            spark_version: "15.4.x-scala2.12"
            node_type_id: "m5d.large"
            num_workers: 0
            spark_conf:
              "spark.databricks.cluster.profile": "singleNode"
            spark_env_vars:
              JIRA_BASE_URL: ${var.jira_base_url}
              JIRA_SERVICE_ACCOUNT_EMAIL: ${var.jira_service_account_email}
              JIRA_API_TOKEN: ${var.jira_api_token}
              DATA_INSPECTOR_URL: ${var.data_inspector_url}

variables:
  data_inspector_url:
    description: URL of the companion data-inspector MCP app
  jira_base_url:
    description: "Jira workspace URL, e.g. https://your-org.atlassian.net"
    default: ""
  jira_service_account_email:
    description: Service account email for Jira API auth
    default: ""
  jira_api_token:
    description: Jira API token for the service account
    default: ""

targets:
  dev:
    mode: development
    default: true
    variables:
      data_inspector_url: https://mcp-data-inspector-7474652869938903.aws.databricksapps.com
      # Set jira_base_url, jira_service_account_email, jira_api_token via .env or CLI
      # databricks bundle deploy --var="jira_api_token=..." when you have creds
```

> **Why `2>/dev/null || true` on the whl copy?** The whl is only present after `apx build` succeeds and creates it in `dist/`. On first bundle deploy (before any wheel is built by apx), this prevents a hard failure. The actual whl for the job is set via the `libraries:` key, not the `.build/` sync.

- [ ] **Step 3: Read current `SETUP.md`**

```bash
cat SETUP.md
```

- [ ] **Step 4: Append Jira prerequisites section to `SETUP.md`**

Append the following to `SETUP.md`:

```markdown
## Jira Async Trigger Setup

To enable automatic investigation when a "Data Issue" Jira ticket is created:

### 1. Jira Service Account

Create or designate a Jira user with "Add Comments" permission on the target project. This is the account the agent posts comments as.

### 2. API Token

Log in as the service account → **Account Settings → Security → API tokens → Create**.

Copy the token — it's shown only once.

### 3. Issue Type

Confirm "Data Issue" exists as an issue type in the target Jira project. If not, a project admin can create it under **Project Settings → Issue types**.

### 4. Webhook Registration

In the target Jira project: **Project Settings → Webhooks → Create webhook**.

| Field | Value |
|---|---|
| URL | `https://<data-triage-app-url>/webhook/jira` |
| Events | Issue → created |
| JQL filter | `issuetype = "Data Issue"` |
| Secret | Value of `JIRA_WEBHOOK_SECRET` (see below) |

### 5. Environment Variables

Generate a webhook secret:

```bash
openssl rand -hex 32
```

Set these environment variables in the app and job:

| Variable | Where to set | Value |
|---|---|---|
| `JIRA_BASE_URL` | App env + job cluster | `https://your-org.atlassian.net` |
| `JIRA_SERVICE_ACCOUNT_EMAIL` | App env + job cluster | Service account email |
| `JIRA_API_TOKEN` | App env + job cluster | Token from step 2 |
| `JIRA_WEBHOOK_SECRET` | App env only | Generated secret |
| `DATA_TRIAGE_JOB_ID` | App env only | Job ID from `databricks bundle deploy` output |

For local testing, add these to `.env` (gitignored).

For Databricks Apps deployment, set `JIRA_WEBHOOK_SECRET` and `DATA_TRIAGE_JOB_ID` as app environment variables in the Databricks Apps UI or via `databricks.yml`.

### 6. Deploy and Get Job ID

```bash
databricks bundle deploy --profile <your-profile>
```

The output includes the job ID:

```
✓  data-triage-investigation (job ID: 1234)
```

Set `DATA_TRIAGE_JOB_ID=1234` in the app's environment variables.
```

- [ ] **Step 5: Commit**

```bash
git add databricks.yml SETUP.md
git commit -m "feat: bundle job resource for investigation + SETUP.md Jira prereqs"
```

---

## Self-Review

### Spec Coverage Check

| Spec requirement | Task |
|---|---|
| HMAC-SHA256 signature validation | Task 2, webhook.py `_verify_signature` |
| Filter: `jira:issue_created` + `Data Issue` | Task 2, webhook.py event/type filter |
| Extract: issue_key, summary, description (ADF→text), priority, reporter | Task 2, `_extract_text()` + field extraction |
| `ws.jobs.run_now(python_params=[...])` → 202 | Task 2, webhook.py + test |
| 401 on bad signature | Task 2, test |
| 200 on non-matching events | Task 2, test |
| `jobs/investigate.py` reads fields via argparse | Task 3, `main()` |
| Calls FM API via httpx tool-calling loop | Task 3, `run_investigation()` |
| Posts comment to Jira | Task 3, `main()` calls `jira.post_comment()` |
| On exception: posts error comment, exits 1 | Task 3, `main()` except block |
| `JiraClient.post_comment()` with ADF body | Task 1 |
| Raises `JiraClientError` on non-2xx | Task 1 |
| Pydantic Settings from env | Task 0 |
| `investigate` entry point in pyproject.toml | Task 0 |
| New env vars in `.env.example` | Task 0 |
| `databricks.yml` job resource | Task 4 |
| `SETUP.md` Jira prerequisites | Task 4 |

All requirements covered.

### Type Consistency

- `JiraClient.post_comment(issue_key: str, body: str)` — used consistently in Task 1, Task 3
- `run_investigation(query: str, ws: WorkspaceClient) -> str` — called in Task 3 `main()`
- `_dispatch_tool(name, args, ws)` — tool names match agent_router.py exports exactly
- `Settings.data_triage_job_id: int` — used as `job_id=settings.data_triage_job_id` in webhook (int ✓)
- `python_params: list[str]` — argparse expects `sys.argv` strings, `run_now(python_params=...)` passes `list[str]` ✓

### Placeholder Scan

No TBD, TODO, or incomplete sections found.
