from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from mlflow.entities import AssessmentSource, AssessmentSourceType
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import NOT_FOUND, PERMISSION_DENIED

from apx_agent import _trace_feedback_api
from apx_agent._trace_feedback import (
    IDEMPOTENCY_METADATA_KEY,
    TraceFeedbackResult,
    TraceFeedbackView,
)


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
            source=AssessmentSource(
                source_type=AssessmentSourceType.HUMAN,
                source_id="reviewer@example.com",
            ),
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
            raise AssertionError("get_trace fetches spans and is unsupported")

        def get_trace_info(self, trace_id):
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


@pytest.mark.unit
def test_obo_api_reads_trace_info_without_fetching_spans() -> None:
    info = SimpleNamespace(trace_id="tr-1", tags={}, assessments=[])
    calls = []

    class FakeStore:
        def __init__(self, get_host_creds):
            pass

        def get_trace(self, trace_id):
            raise AssertionError("get_trace fetches spans and is unsupported")

        def get_trace_info(self, trace_id):
            calls.append(trace_id)
            return info

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        api = _trace_feedback_api._OBOTraceFeedbackApi(
            host="https://workspace.example",
            token="user-token",
        )

    assert api.get_trace("tr-1").info is info
    assert calls == ["tr-1"]


@pytest.mark.unit
def test_obo_api_propagates_trace_info_errors() -> None:
    class FakeStore:
        def __init__(self, get_host_creds):
            pass

        def get_trace(self, trace_id):
            raise AssertionError("get_trace fetches spans and is unsupported")

        def get_trace_info(self, trace_id):
            raise MlflowException("denied", error_code=PERMISSION_DENIED)

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        api = _trace_feedback_api._OBOTraceFeedbackApi(
            host="https://workspace.example",
            token="user-token",
        )

    with pytest.raises(MlflowException, match="denied"):
        api.get_trace("tr-1")


def _feedback_app() -> FastAPI:
    app = FastAPI()
    app.include_router(_trace_feedback_api.build_trace_feedback_router())
    return app


@pytest.mark.asyncio
async def test_deployed_feedback_requires_obo_and_human_identity(monkeypatch) -> None:
    monkeypatch.setenv("DATABRICKS_APP_NAME", "feedback-app")
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
async def test_deployed_feedback_maps_missing_mlflow_adapter_to_503(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_APP_NAME", "feedback-app")
    monkeypatch.setenv("DATABRICKS_HOST", "https://trusted.example")
    with patch.dict(
        "sys.modules",
        {"mlflow.store.tracking.databricks_rest_store": None},
    ):
        async with AsyncClient(
            transport=ASGITransport(
                app=_feedback_app(),
                raise_app_exceptions=False,
            ),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/_apx/feedback/tr-1",
                headers={
                    "X-Forwarded-Access-Token": "user-token",
                    "X-Forwarded-Email": "reviewer@example.com",
                },
            )

    assert response.status_code == 503
    assert response.json() == {"detail": "Trace feedback requires the APX eval extra."}
    assert "user-token" not in response.text


@pytest.mark.asyncio
async def test_deployed_feedback_uses_trusted_host_and_forwarded_email(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_APP_NAME", "feedback-app")
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
async def test_deployed_feedback_reads_and_replays_with_request_scoped_trace_info(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_APP_NAME", "feedback-app")
    monkeypatch.setenv("DATABRICKS_HOST", "https://trusted.example")
    captured = {"creds": [], "reads": [], "writes": []}
    info = SimpleNamespace(
        trace_id="tr-1",
        tags={"team": "claims"},
        assessments=[
            {
                "assessment_id": "a-existing",
                "assessment_name": "quality",
                "feedback": {"value": 4},
                "source": {
                    "source_type": "HUMAN",
                    "source_id": "reviewer@example.com",
                },
                "metadata": {IDEMPOTENCY_METADATA_KEY: "req-1"},
            }
        ],
    )

    class FakeStore:
        def __init__(self, get_host_creds):
            captured["creds"].append(get_host_creds())

        def get_trace(self, trace_id):
            raise AssertionError("get_trace fetches spans and is unsupported")

        def get_trace_info(self, trace_id):
            captured["reads"].append(trace_id)
            return info

        def create_assessment(self, assessment):
            captured["writes"].append(assessment)
            return assessment

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=_feedback_app()),
            base_url="http://test",
            headers={
                "X-Forwarded-Access-Token": "user-token",
                "X-Forwarded-Email": "reviewer@example.com",
            },
        ) as client:
            loaded = await client.get("/_apx/feedback/tr-1")
            replayed = await client.post(
                "/_apx/feedback",
                json={
                    "trace_id": "tr-1",
                    "name": "quality",
                    "value": 4,
                    "idempotency_key": "req-1",
                },
            )

    assert loaded.status_code == 200
    assert loaded.json()["tags"] == {"team": "claims"}
    assert loaded.json()["assessments"][0]["assessment_id"] == "a-existing"
    assert replayed.status_code == 200
    assert replayed.json() == {
        "trace_id": "tr-1",
        "feedback_id": "a-existing",
        "name": "quality",
        "created": False,
    }
    assert captured["reads"] == ["tr-1", "tr-1"]
    assert captured["writes"] == []
    assert all(creds.host == "https://trusted.example" for creds in captured["creds"])
    assert all(creds.token == "user-token" for creds in captured["creds"])


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


@pytest.mark.asyncio
async def test_local_feedback_writes_and_reads_with_existing_helpers(
    monkeypatch,
) -> None:
    writes = []
    view = TraceFeedbackView(trace_id="tr-1", tags={}, assessments=[])
    monkeypatch.setattr(
        _trace_feedback_api,
        "attach_feedback",
        lambda feedback, mlflow_api=None: (
            writes.append((feedback, mlflow_api)),
            TraceFeedbackResult(
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


@pytest.mark.asyncio
async def test_setup_agent_mounts_feedback_once_when_dev_ui_disabled(
    monkeypatch,
) -> None:
    from apx_agent import AgentConfig, LlmAgent, setup_agent

    from .conftest import get_weather

    monkeypatch.setenv("APX_DEV_UI", "0")
    app = FastAPI()
    await setup_agent(app, LlmAgent(tools=[get_weather]), AgentConfig(name="t"))
    await setup_agent(app, LlmAgent(tools=[get_weather]), AgentConfig(name="t"))

    paths = [route.path for route in app.routes if isinstance(route, APIRoute)]
    assert paths.count("/_apx/feedback") == 1
    assert paths.count("/_apx/feedback/{trace_id:path}") == 1


def test_create_app_mounts_feedback_through_lifespan(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from apx_agent import AgentConfig, LlmAgent, create_app

    from .conftest import get_weather

    monkeypatch.setenv("APX_DEV_UI", "0")
    app = create_app(LlmAgent(tools=[get_weather]), AgentConfig(name="t"))
    with patch("apx_agent._wiring._make_workspace_client"), TestClient(app):
        paths = [route.path for route in app.routes if isinstance(route, APIRoute)]
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
        paths = [route.path for route in app.routes if isinstance(route, APIRoute)]
    assert "/_apx/feedback" in paths
    assert "/_apx/feedback/{trace_id:path}" in paths
