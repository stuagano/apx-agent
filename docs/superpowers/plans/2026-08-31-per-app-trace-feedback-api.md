# Per-App Trace Feedback API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mount an authenticated MLflow trace-feedback API in every APX FastAPI application, using the signed-in Databricks Apps user through request-scoped OBO credentials.

**Architecture:** Add one private FastAPI router that delegates to the existing trace-feedback helpers and is mounted through the shared `setup_agent()` path used by both public runtimes. Deployed Apps construct a request-scoped adapter over the pinned MLflow Databricks tracing store; local development keeps the existing ambient MLflow behavior.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic 2, MLflow `>=3.14,<3.15`, Databricks Apps OBO headers, pytest, httpx

**Spec:** `docs/superpowers/specs/2026-08-31-per-app-trace-feedback-api-design.md`

## Global Constraints

- Keep MLflow assessments as the only persistence mechanism.
- Mount `POST /_apx/feedback` and `GET /_apx/feedback/{trace_id:path}` independently of `APX_DEV_UI`.
- In Databricks Apps, require user OBO; never fall back to the app service principal or `APX_DEV_UI_TOKEN`.
- Resolve the workspace API host from `DATABRICKS_HOST`, never from in-App `X-Forwarded-Host`.
- Derive the human assessment source from `X-Forwarded-Email`, falling back to `X-Forwarded-User`; never accept source identity in the request body.
- Keep access tokens request-scoped and out of response bodies and logs; never mutate process environment for authentication.
- Preserve existing CLI and local-helper behavior.
- Keep idempotency explicitly best-effort; do not claim atomic exactly-once behavior.
- Add no database, queue, cache, scheduler, UI, central service, or service-principal submission path.
- Do not deploy a Databricks App as part of this plan.

---

## File Map

- Modify `python/src/apx_agent/_trace_feedback.py` to distinguish missing traces and unavailable MLflow support from validation failures without changing existing helper inputs or results.
- Create `python/src/apx_agent/_trace_feedback_api.py` to own the strict HTTP body, request authentication, request-scoped MLflow adapter, error translation, and router factory.
- Modify `python/src/apx_agent/_wiring.py` to include the router once through shared agent setup.
- Modify `python/tests/test_trace_feedback.py` to cover the precise missing-trace exception.
- Create `python/tests/test_trace_feedback_api.py` to cover adapter credentials, HTTP behavior, security boundaries, and both runtime mounting paths.
- Modify `docs/evaluate/overview.md` to document the per-app endpoint and its user/OBO boundary next to the existing CLI workflow.

### Task 1: Make Feedback Failures Explicit

**Files:**
- Modify: `python/src/apx_agent/_trace_feedback.py:16`
- Modify: `python/src/apx_agent/_trace_feedback.py:127`
- Test: `python/tests/test_trace_feedback.py:150`

**Interfaces:**
- Consumes: `TraceFeedbackError` and `get_feedback_view(trace_id: str, *, mlflow_api: Any = None) -> TraceFeedbackView`
- Produces: `TraceNotFoundError(TraceFeedbackError)`, raised only when `get_feedback_view()` receives no trace
- Produces: `TraceFeedbackUnavailableError(TraceFeedbackError)`, raised when the optional MLflow dependency is missing

- [ ] **Step 1: Tighten the existing missing-trace test**

Change the final test in `python/tests/test_trace_feedback.py` to require the precise exception, and add a dependency-absence test:

```python
@pytest.mark.unit
def test_get_feedback_view_rejects_missing_trace() -> None:
    api = SimpleNamespace(get_trace=lambda trace_id: None)
    with pytest.raises(_trace_feedback.TraceNotFoundError, match="not found"):
        _trace_feedback.get_feedback_view("tr-missing", mlflow_api=api)


@pytest.mark.unit
def test_default_mlflow_api_reports_unavailable_dependency() -> None:
    with patch.dict("sys.modules", {"mlflow": None}):
        with pytest.raises(
            _trace_feedback.TraceFeedbackUnavailableError,
            match="requires mlflow",
        ):
            _trace_feedback._default_mlflow_api()
```

Add `from unittest.mock import patch` to the test imports.

- [ ] **Step 2: Run the focused test and verify red**

Run:

```bash
cd python
uv run --frozen --extra eval pytest tests/test_trace_feedback.py \
  -k "missing_trace or unavailable_dependency" -q
```

Expected: FAIL because the two precise exception classes do not exist.

- [ ] **Step 3: Add the narrow exception and raise it**

Add immediately after `TraceFeedbackError` in `python/src/apx_agent/_trace_feedback.py`:

```python
class TraceNotFoundError(TraceFeedbackError):
    """Raised when an MLflow trace does not exist."""


class TraceFeedbackUnavailableError(TraceFeedbackError):
    """Raised when optional MLflow feedback support is unavailable."""
```

Change `_default_mlflow_api()` to raise the precise unavailable error while
retaining its existing message:

```python
except ImportError as exc:
    raise TraceFeedbackUnavailableError(
        "trace feedback requires mlflow; install 'apx-agent[eval]'"
    ) from exc
```

Change the missing-trace branch in `get_feedback_view()`:

```python
if trace is None:
    raise TraceNotFoundError(f"MLflow trace {trace_id!r} not found")
```

Do not change any other validation exception or publicly export the new class.

- [ ] **Step 4: Run all helper tests**

Run:

```bash
cd python
uv run --frozen --extra eval pytest tests/test_trace_feedback.py -q
```

Expected: all tests in `test_trace_feedback.py` PASS.

- [ ] **Step 5: Commit the precise error contract**

```bash
git add python/src/apx_agent/_trace_feedback.py python/tests/test_trace_feedback.py
git commit -m "refactor: distinguish missing feedback traces"
```

### Task 2: Add the Request-Scoped OBO Adapter

**Files:**
- Create: `python/src/apx_agent/_trace_feedback_api.py`
- Create: `python/tests/test_trace_feedback_api.py`

**Interfaces:**
- Consumes: MLflow `Feedback`, `DatabricksTracingRestStore`, and `MlflowHostCreds`
- Produces: `_OBOTraceFeedbackApi(host: str, token: str)` with `get_trace(trace_id: str)` and `log_feedback(**kwargs)` methods

- [ ] **Step 1: Write adapter credential and persistence tests**

Create `python/tests/test_trace_feedback_api.py` with imports and focused fake-store tests:

```python
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import NOT_FOUND, PERMISSION_DENIED

from apx_agent import _trace_feedback_api


@pytest.mark.unit
def test_obo_api_binds_host_and_token_per_instance() -> None:
    captured = {}

    class FakeStore:
        def __init__(self, get_host_creds):
            captured["creds"] = get_host_creds()

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        _trace_feedback_api._OBOTraceFeedbackApi(
            host="https://workspace.example",
            token="user-token",
        )

    assert captured["creds"].host == "https://workspace.example"
    assert captured["creds"].token == "user-token"


@pytest.mark.unit
def test_obo_api_logs_feedback_through_databricks_tracing_store() -> None:
    calls = []

    class FakeStore:
        def __init__(self, get_host_creds):
            self.creds = get_host_creds()

        def create_assessment(self, assessment):
            calls.append(assessment)
            assessment.assessment_id = "a-1"
            return assessment

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        api = _trace_feedback_api._OBOTraceFeedbackApi(
            host="https://workspace.example",
            token="user-token",
        )
        result = api.log_feedback(
            trace_id="tr-1",
            name="quality",
            value=4,
            rationale="Grounded",
            source=SimpleNamespace(),
            metadata={"feature": "claims"},
        )

    assert result.assessment_id == "a-1"
    assert calls[0].trace_id == "tr-1"
    assert calls[0].name == "quality"
    assert calls[0].value == 4
    assert calls[0].rationale == "Grounded"
    assert calls[0].metadata == {"feature": "claims"}


@pytest.mark.unit
def test_obo_api_converts_store_not_found_to_none() -> None:
    class FakeStore:
        def __init__(self, get_host_creds):
            pass

        def get_trace(self, trace_id):
            raise MlflowException("missing", error_code=NOT_FOUND)

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        api = _trace_feedback_api._OBOTraceFeedbackApi(
            host="https://workspace.example",
            token="user-token",
        )

    assert api.get_trace("tr-missing") is None
```

- [ ] **Step 2: Run the adapter tests and verify red**

Run:

```bash
cd python
uv run --frozen --extra eval pytest tests/test_trace_feedback_api.py -k "obo_api" -q
```

Expected: FAIL because `apx_agent._trace_feedback_api` does not exist.

- [ ] **Step 3: Implement the private request-scoped adapter**

Create `python/src/apx_agent/_trace_feedback_api.py` with the adapter first:

```python
"""Authenticated per-app HTTP access to MLflow trace feedback."""

from __future__ import annotations

from typing import Any

class _OBOTraceFeedbackApi:
    def __init__(self, *, host: str, token: str) -> None:
        from mlflow.store.tracking.databricks_rest_store import (
            DatabricksTracingRestStore,
        )
        from mlflow.utils.rest_utils import MlflowHostCreds

        self._store = DatabricksTracingRestStore(
            lambda: MlflowHostCreds(host=host, token=token)
        )

    def get_trace(self, trace_id: str) -> Any:
        from mlflow.exceptions import MlflowException

        try:
            return self._store.get_trace(trace_id)
        except MlflowException as exc:
            if exc.get_http_status_code() == 404:
                return None
            raise

    def log_feedback(self, **kwargs: Any) -> Any:
        from mlflow.entities import Feedback

        assessment = Feedback(
            trace_id=kwargs["trace_id"],
            name=kwargs["name"],
            value=kwargs["value"],
            rationale=kwargs.get("rationale"),
            source=kwargs.get("source"),
            metadata=kwargs.get("metadata"),
        )
        return self._store.create_assessment(assessment)
```

Keep this class private. Do not cache instances, store credentials on FastAPI
application state, mutate MLflow globals, or modify environment variables.
Keep every MLflow import inside a method so importing and mounting the router
still works in a core-only APX installation.

- [ ] **Step 4: Run the adapter tests and verify green**

Run:

```bash
cd python
uv run --frozen --extra eval pytest tests/test_trace_feedback_api.py -k "obo_api" -q
```

Expected: all three adapter tests PASS.

- [ ] **Step 5: Commit the OBO adapter**

```bash
git add python/src/apx_agent/_trace_feedback_api.py python/tests/test_trace_feedback_api.py
git commit -m "feat: add request-scoped feedback client"
```

### Task 3: Add the Authenticated Router and Shared Mount

**Files:**
- Modify: `python/src/apx_agent/_trace_feedback_api.py`
- Modify: `python/src/apx_agent/_wiring.py:582`
- Modify: `python/tests/test_trace_feedback_api.py`

**Interfaces:**
- Consumes: `_OBOTraceFeedbackApi`, `extract_obo_headers()`, `_in_databricks_app()`, and existing feedback helpers
- Produces: `build_trace_feedback_router() -> APIRouter`
- Produces: `_mount_trace_feedback_routes(app: FastAPI) -> None`
- Mounts: `POST /_apx/feedback` and `GET /_apx/feedback/{trace_id:path}` exactly once per FastAPI instance

- [ ] **Step 1: Write failing request-authentication tests**

Append helpers and tests to `python/tests/test_trace_feedback_api.py`:

```python
def _feedback_app() -> FastAPI:
    app = FastAPI()
    app.include_router(_trace_feedback_api.build_trace_feedback_router())
    return app


@pytest.mark.asyncio
async def test_deployed_feedback_requires_obo_and_human_identity(monkeypatch) -> None:
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.setenv("DATABRICKS_HOST", "https://trusted.example")
    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        missing_token = await client.get("/_apx/feedback/tr-1")
        missing_identity = await client.get(
            "/_apx/feedback/tr-1",
            headers={"X-Forwarded-Access-Token": "secret"},
        )

    assert missing_token.status_code == 401
    assert missing_identity.status_code == 401
    assert "secret" not in missing_identity.text


@pytest.mark.asyncio
async def test_deployed_feedback_uses_trusted_host_and_forwarded_email(monkeypatch) -> None:
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.setenv("DATABRICKS_HOST", "https://trusted.example")
    captured = {}

    def fake_api(*, host, token):
        captured.update(host=host, token=token)
        return SimpleNamespace(
            log_feedback=lambda **kwargs: (
                captured.update(write=kwargs),
                SimpleNamespace(assessment_id="a-1"),
            )[1]
        )

    monkeypatch.setattr(_trace_feedback_api, "_OBOTraceFeedbackApi", fake_api)
    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/_apx/feedback",
            headers={
                "X-Forwarded-Access-Token": "user-token",
                "X-Forwarded-Email": "reviewer@example.com",
                "X-Forwarded-Host": "attacker.example",
            },
            json={"trace_id": "tr-1", "name": "quality", "value": 4},
        )

    assert response.status_code == 200
    assert captured["host"] == "https://trusted.example"
    assert captured["token"] == "user-token"
    assert captured["write"]["source"].source_id == "reviewer@example.com"


@pytest.mark.asyncio
async def test_feedback_body_rejects_source_override() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/_apx/feedback",
            json={
                "trace_id": "tr-1",
                "name": "quality",
                "value": True,
                "source": "spoofed-user",
            },
        )

    assert response.status_code == 422
```

- [ ] **Step 2: Write failing behavior and error-mapping tests**

Append these tests, using monkeypatches so they make no workspace calls:

```python
@pytest.mark.asyncio
async def test_local_feedback_writes_and_reads_with_existing_helpers(monkeypatch) -> None:
    writes = []
    view = SimpleNamespace(trace_id="tr-1", tags={}, assessments=[])
    monkeypatch.setattr(
        _trace_feedback_api,
        "attach_feedback",
        lambda feedback, mlflow_api=None: (
            writes.append((feedback, mlflow_api)),
            SimpleNamespace(
                trace_id=feedback.trace_id,
                feedback_id="a-1",
                name=feedback.name,
                created=True,
            ),
        )[1],
    )
    monkeypatch.setattr(
        _trace_feedback_api,
        "get_feedback_view",
        lambda trace_id, mlflow_api=None: view,
    )

    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/_apx/feedback",
            json={
                "trace_id": "tr-1",
                "name": "quality",
                "value": True,
                "evidence": {"screenshot_uri": "s3://bucket/review.png"},
            },
        )
        loaded = await client.get("/_apx/feedback/tr-1")

    assert created.status_code == 200
    assert loaded.status_code == 200
    assert writes[0][0].source == "apx.trace_feedback"
    assert writes[0][1] is None
    assert loaded.json() == {"trace_id": "tr-1", "tags": {}, "assessments": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [(PERMISSION_DENIED, 403), (NOT_FOUND, 404)],
)
async def test_feedback_maps_mlflow_client_errors(
    monkeypatch, error_code, expected_status
) -> None:
    monkeypatch.setattr(
        _trace_feedback_api,
        "get_feedback_view",
        lambda trace_id, mlflow_api=None: (_ for _ in ()).throw(
            MlflowException("sensitive upstream detail", error_code=error_code)
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        response = await client.get("/_apx/feedback/tr-1")

    assert response.status_code == expected_status
    assert "sensitive upstream detail" not in response.text
```

Add the precise helper-error and generic-upstream cases:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (_trace_feedback_api.TraceNotFoundError("missing"), 404),
        (
            _trace_feedback_api.TraceFeedbackUnavailableError(
                "trace feedback requires mlflow"
            ),
            503,
        ),
        (MlflowException("token=user-token"), 502),
    ],
)
async def test_feedback_sanitizes_helper_and_upstream_errors(
    monkeypatch, error, expected_status
) -> None:
    monkeypatch.setattr(
        _trace_feedback_api,
        "get_feedback_view",
        lambda trace_id, mlflow_api=None: (_ for _ in ()).throw(error),
    )
    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        response = await client.get("/_apx/feedback/tr-1")

    assert response.status_code == expected_status
    assert "user-token" not in response.text
```

- [ ] **Step 3: Run the router tests and verify red**

Run:

```bash
cd python
uv run --frozen --extra eval pytest tests/test_trace_feedback_api.py -k "not obo_api" -q
```

Expected: FAIL because `build_trace_feedback_router()` does not exist.

- [ ] **Step 4: Implement the strict body and request context**

Add these imports and types to `python/src/apx_agent/_trace_feedback_api.py`:

```python
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr

from ._obo import _in_databricks_app, extract_obo_headers
from ._trace_feedback import (
    DEFAULT_SOURCE_ID,
    TraceFeedback,
    TraceFeedbackError,
    TraceFeedbackUnavailableError,
    TraceNotFoundError,
    attach_feedback,
    get_feedback_view,
)


class _TraceFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: StrictStr
    name: StrictStr
    value: StrictBool | StrictInt | StrictFloat | StrictStr
    comment: StrictStr | None = None
    idempotency_key: StrictStr | None = None
    evidence: dict[StrictStr, StrictStr] | None = None


def _request_feedback_context(request: Request) -> tuple[Any | None, str]:
    if not _in_databricks_app():
        return None, DEFAULT_SOURCE_ID

    obo = extract_obo_headers(headers=request.headers)
    token = obo.get("user_token")
    host = obo.get("workspace_host")
    source = obo.get("user_email") or obo.get("user_id")
    if not token or not host or not source:
        raise HTTPException(
            status_code=401,
            detail="Trace feedback requires an authenticated Databricks Apps user.",
        )
    return _OBOTraceFeedbackApi(host=host, token=token), source
```

The generic `401` deliberately does not identify which credential or forwarded
header was missing.

- [ ] **Step 5: Implement sanitized error translation and routes**

Add to `python/src/apx_agent/_trace_feedback_api.py`:

```python
def _raise_http_error(exc: Exception) -> NoReturn:
    if isinstance(exc, TraceNotFoundError):
        raise HTTPException(status_code=404, detail="Trace not found.") from exc
    if isinstance(exc, TraceFeedbackUnavailableError):
        raise HTTPException(
            status_code=503,
            detail="Trace feedback requires the APX eval extra.",
        ) from exc
    if isinstance(exc, TraceFeedbackError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from mlflow.exceptions import MlflowException

    if isinstance(exc, MlflowException):
        status = exc.get_http_status_code()
        if status == 401:
            raise HTTPException(status_code=401, detail="MLflow authentication failed.") from exc
        if status == 403:
            raise HTTPException(status_code=403, detail="Trace access denied.") from exc
        if status == 404:
            raise HTTPException(status_code=404, detail="Trace not found.") from exc
    raise HTTPException(status_code=502, detail="MLflow trace feedback request failed.") from exc


def build_trace_feedback_router() -> APIRouter:
    router = APIRouter(prefix="/_apx/feedback", tags=["trace-feedback"])

    @router.post("")
    def post_feedback(body: _TraceFeedbackRequest, request: Request) -> Any:
        api, source = _request_feedback_context(request)
        try:
            return attach_feedback(
                TraceFeedback(
                    trace_id=body.trace_id,
                    name=body.name,
                    value=body.value,
                    comment=body.comment,
                    source=source,
                    idempotency_key=body.idempotency_key,
                    evidence=body.evidence,
                ),
                mlflow_api=api,
            )
        except Exception as exc:
            _raise_http_error(exc)

    @router.get("/{trace_id:path}")
    def get_feedback(trace_id: str, request: Request) -> Any:
        api, _ = _request_feedback_context(request)
        try:
            return get_feedback_view(trace_id, mlflow_api=api)
        except Exception as exc:
            _raise_http_error(exc)

    return router
```

Import `NoReturn` from `typing`. The MLflow exception import stays inside
`_raise_http_error()` so module import remains safe without the optional
dependency. After implementation, use the repository's configured formatter rather than
manually retaining any long lines shown in the plan.

- [ ] **Step 6: Run the full API test file**

Run:

```bash
cd python
uv run --frozen --extra eval pytest tests/test_trace_feedback_api.py -q
```

Expected: all adapter, authentication, behavior, and error tests PASS.

- [ ] **Step 7: Write failing shared-mount tests**

Add tests to `python/tests/test_trace_feedback_api.py` using existing APX test
helpers (`AgentConfig`, `LlmAgent`, and `.conftest.get_weather`):

```python
@pytest.mark.asyncio
async def test_setup_agent_mounts_feedback_once_when_dev_ui_disabled(monkeypatch) -> None:
    from apx_agent import AgentConfig, LlmAgent, setup_agent
    from .conftest import get_weather

    monkeypatch.setenv("APX_DEV_UI", "0")
    app = FastAPI()
    await setup_agent(app, LlmAgent(tools=[get_weather]), AgentConfig(name="t"))
    await setup_agent(app, LlmAgent(tools=[get_weather]), AgentConfig(name="t"))

    paths = [route.path for route in app.routes]
    assert paths.count("/_apx/feedback") == 1
    assert paths.count("/_apx/feedback/{trace_id:path}") == 1


def test_create_app_mounts_feedback_through_lifespan(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from apx_agent import AgentConfig, LlmAgent, create_app
    from .conftest import get_weather

    monkeypatch.setenv("APX_DEV_UI", "0")
    app = create_app(LlmAgent(tools=[get_weather]), AgentConfig(name="t"))
    with TestClient(app):
        paths = [route.path for route in app.routes]
    assert "/_apx/feedback" in paths
    assert "/_apx/feedback/{trace_id:path}" in paths


def test_mount_mcp_endpoints_mounts_feedback_through_startup(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from apx_agent import AgentConfig, LlmAgent, mount_mcp_endpoints
    from .conftest import get_weather

    monkeypatch.setenv("APX_DEV_UI", "0")
    app = FastAPI()
    mount_mcp_endpoints(app, LlmAgent(tools=[get_weather]), AgentConfig(name="t"))
    with TestClient(app):
        paths = [route.path for route in app.routes]
    assert "/_apx/feedback" in paths
    assert "/_apx/feedback/{trace_id:path}" in paths
```

- [ ] **Step 8: Run mount tests and verify red**

Run:

```bash
cd python
uv run --frozen --extra eval pytest tests/test_trace_feedback_api.py -k "mounts_feedback" -q
```

Expected: FAIL because `_wiring.setup_agent()` does not mount the router.

- [ ] **Step 9: Mount once in the shared setup path**

Add a private helper near the other mounting helpers in
`python/src/apx_agent/_wiring.py`:

```python
def _mount_trace_feedback_routes(app: FastAPI) -> None:
    if getattr(app.state, "trace_feedback_routes_mounted", False):
        return
    from ._trace_feedback_api import build_trace_feedback_router

    app.include_router(build_trace_feedback_router())
    app.state.trace_feedback_routes_mounted = True
```

Call `_mount_trace_feedback_routes(app)` at the beginning of `setup_agent()`,
before config discovery can return `None` and before optional dev-UI mounting.
Do not add separate calls to `create_app()` or `mount_mcp_endpoints()`; both
already converge on `setup_agent()`.

- [ ] **Step 10: Run helper, API, and wiring tests**

Run:

```bash
cd python
uv run --frozen --extra eval pytest \
  tests/test_trace_feedback.py \
  tests/test_trace_feedback_api.py \
  tests/test_wiring.py \
  tests/test_root_chat.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 11: Commit the router and shared mounting**

```bash
git add \
  python/src/apx_agent/_trace_feedback_api.py \
  python/src/apx_agent/_wiring.py \
  python/tests/test_trace_feedback_api.py
git commit -m "feat: mount per-app trace feedback API"
```

### Task 4: Document the Per-App Integration and Run the Full Gate

**Files:**
- Modify: `docs/evaluate/overview.md:64`
- Verify: `docs/superpowers/specs/2026-08-31-per-app-trace-feedback-api-design.md`

**Interfaces:**
- Consumes: the two HTTP routes and authentication behavior delivered by Task 3
- Produces: user-facing integration instructions that distinguish CLI ambient credentials from deployed per-app OBO

- [ ] **Step 1: Add the deployed-app HTTP workflow**

After the CLI examples in `docs/evaluate/overview.md`, add:

````markdown
### Per-app HTTP API

Every APX FastAPI application exposes the same trace-feedback routes after it
upgrades and redeploys:

```http
POST /_apx/feedback
Content-Type: application/json

{
  "trace_id": "tr-123",
  "name": "domain_quality",
  "value": 4,
  "comment": "Correct answer, weak rationale",
  "idempotency_key": "review-row-123",
  "evidence": {
    "screenshot_uri": "s3://bucket/review.png",
    "feature": "claims_search"
  }
}
```

```http
GET /_apx/feedback/tr-123
```

Call these routes through the Databricks Apps gateway as a signed-in user. The
gateway supplies the OBO token and user identity; APX writes and reads MLflow as
that user. The JSON body cannot select a source identity, workspace host, or
service principal. Missing OBO fails closed, and `APX_DEV_UI_TOKEN` does not
authorize this API.

The endpoints remain available when `APX_DEV_UI=0`. Existing deployments must
upgrade APX and redeploy before they expose the routes.
````

Keep the existing CLI authentication section intact because CLI calls continue
to use the active MLflow configuration.

- [ ] **Step 2: Run documentation and formatting checks**

Run:

```bash
pre-commit run --files \
  python/src/apx_agent/_trace_feedback.py \
  python/src/apx_agent/_trace_feedback_api.py \
  python/src/apx_agent/_wiring.py \
  python/tests/test_trace_feedback.py \
  python/tests/test_trace_feedback_api.py \
  docs/evaluate/overview.md
```

Expected: all configured hooks PASS. If an auto-fixing hook changes a file,
review that diff and rerun the same command.

- [ ] **Step 3: Build the fresh-worktree TypeScript artifacts**

Run:

```bash
cd typescript
npm ci
npm run build
```

Expected: install and build PASS. Generated build artifacts remain ignored.

- [ ] **Step 4: Run the repository read-after-write gate**

Run from the repository root:

```bash
make check
```

Expected: the complete Python suite, including `*_reality_ctk.py`, passes and
the lockfile registry sanitizer reports a clean result.

- [ ] **Step 5: Verify the final diff and worktree**

Run:

```bash
git diff --check
git status --short --branch -uall
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- \
  python/src/apx_agent/_trace_feedback.py \
  python/src/apx_agent/_trace_feedback_api.py \
  python/src/apx_agent/_wiring.py \
  python/tests/test_trace_feedback.py \
  python/tests/test_trace_feedback_api.py \
  docs/evaluate/overview.md
```

Expected: only the approved trace-feedback API, tests, documentation, spec, and
plan are present; no lockfile, generated artifact, unrelated branch, or
credential file is changed.

- [ ] **Step 6: Commit the integration documentation**

```bash
git add docs/evaluate/overview.md
git commit -m "docs: add per-app feedback integration"
```

- [ ] **Step 7: Recheck the clean final state**

Run:

```bash
git status --short --branch -uall
git log --oneline --decorate --max-count=6
```

Expected: the worktree is clean and the branch contains the design commit plus
the four focused implementation commits described in this plan.
