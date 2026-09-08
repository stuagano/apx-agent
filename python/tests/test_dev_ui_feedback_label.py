"""Tests for the dev UI feedback widget and judge-alignment routes.

Covers:
- POST /_apx/feedback  (submit rating)
- GET  /_apx/feedback/{trace_id}  (read back — button-state persistence)
- POST /_apx/eval/label-start
- POST /_apx/eval/label-align
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard
from apx_agent._trace_feedback import TraceAssessment, TraceFeedbackResult, TraceFeedbackView
from apx_agent._trace_feedback_api import build_trace_feedback_router
import apx_agent._trace_feedback_api as _fb_api


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_ctx(name: str = "test-agent") -> AgentContext:
    config = AgentConfig(name=name, model="claude-fake")
    card = AgentCard(name=name, description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]


def _feedback_app() -> FastAPI:
    app = FastAPI()
    app.include_router(build_trace_feedback_router())
    return app


def _eval_app(experiment_id: str = "exp-1", monkeypatch=None) -> FastAPI:
    app = FastAPI()
    app.state.agent_context = _make_ctx()
    if monkeypatch:
        monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", experiment_id)
    app.include_router(build_dev_ui_router())
    return app


# ── feedback POST → GET round-trip ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.unit
async def test_submit_good_feedback_and_read_back(monkeypatch) -> None:
    """POST quality=true → GET returns the assessment → button state restores."""
    posted: list = []

    def fake_attach(feedback, mlflow_api=None):
        posted.append(feedback)
        return TraceFeedbackResult(
            trace_id=feedback.trace_id,
            feedback_id="a-1",
            name=feedback.name,
            created=True,
        )

    def fake_get_view(trace_id, mlflow_api=None):
        return TraceFeedbackView(
            trace_id=trace_id,
            tags={},
            assessments=[
                TraceAssessment(
                    assessment_id="a-1",
                    name="quality",
                    kind="feedback",
                    value=True,
                    rationale=None,
                    source_type="HUMAN",
                    source_id="apx.trace_feedback",
                )
            ],
        )

    monkeypatch.setattr(_fb_api, "attach_feedback", fake_attach)
    monkeypatch.setattr(_fb_api, "get_feedback_view", fake_get_view)

    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        post_resp = await client.post(
            "/_apx/feedback",
            json={"trace_id": "tr-1", "name": "quality", "value": True},
        )
        get_resp = await client.get("/_apx/feedback/tr-1")

    assert post_resp.status_code == 200
    assert posted[0].name == "quality"
    assert posted[0].value is True

    assert get_resp.status_code == 200
    data = get_resp.json()
    # The UI uses the last quality assessment to restore button state.
    quality = [a for a in data["assessments"] if a["name"] == "quality"]
    assert quality, "quality assessment must be present for button-state restore"
    assert quality[-1]["value"] is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_submit_bad_feedback_round_trip(monkeypatch) -> None:
    """POST quality=false → GET returns value=false."""
    monkeypatch.setattr(
        _fb_api,
        "attach_feedback",
        lambda fb, mlflow_api=None: TraceFeedbackResult(
            trace_id=fb.trace_id, feedback_id="a-2", name=fb.name, created=True
        ),
    )
    monkeypatch.setattr(
        _fb_api,
        "get_feedback_view",
        lambda tid, mlflow_api=None: TraceFeedbackView(
            trace_id=tid,
            tags={},
            assessments=[
                TraceAssessment(
                    assessment_id="a-2",
                    name="quality",
                    kind="feedback",
                    value=False,
                    rationale=None,
                    source_type="HUMAN",
                    source_id="apx.trace_feedback",
                )
            ],
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        post_resp = await client.post(
            "/_apx/feedback",
            json={"trace_id": "tr-2", "name": "quality", "value": False},
        )
        get_resp = await client.get("/_apx/feedback/tr-2")

    assert post_resp.status_code == 200
    data = get_resp.json()
    quality = [a for a in data["assessments"] if a["name"] == "quality"]
    assert quality[-1]["value"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_feedback_no_prior_rating_returns_empty_assessments(monkeypatch) -> None:
    """Trace with no ratings → empty assessments list → buttons render unset."""
    monkeypatch.setattr(
        _fb_api,
        "get_feedback_view",
        lambda tid, mlflow_api=None: TraceFeedbackView(
            trace_id=tid, tags={}, assessments=[]
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_feedback_app()),
        base_url="http://test",
    ) as client:
        resp = await client.get("/_apx/feedback/tr-new")

    assert resp.status_code == 200
    assert resp.json()["assessments"] == []


# ── label-start route ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_start_returns_run_id_and_session_url(monkeypatch) -> None:
    from apx_agent import _labeling

    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-99")

    started: list = []

    def fake_start(**kw):
        started.append(kw)
        return _labeling.StartResult(
            run_id="run-abc",
            session_url="https://databricks.example/review",
            trace_count=12,
            schema_name="quality",
        )

    monkeypatch.setattr(_labeling, "start_session", fake_start)

    with patch("mlflow.set_tracking_uri"):
        app = FastAPI()
        app.state.agent_context = _make_ctx("my-agent")
        app.include_router(build_dev_ui_router())
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/_apx/eval/label-start",
                json={"judge_name": "quality"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["run_id"] == "run-abc"
    assert body["session_url"] == "https://databricks.example/review"
    assert body["trace_count"] == 12
    assert started[0]["experiment_id"] == "exp-99"
    assert started[0]["judge_name"] == "quality"
    assert started[0]["agent_name"] == "my-agent"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_start_requires_judge_name(monkeypatch) -> None:
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-99")
    with patch("mlflow.set_tracking_uri"):
        app = FastAPI()
        app.state.agent_context = _make_ctx()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/_apx/eval/label-start", json={})

    assert resp.status_code == 422
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_start_returns_503_without_experiment_id(monkeypatch) -> None:
    monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    app = FastAPI()
    app.state.agent_context = _make_ctx()
    app.include_router(build_dev_ui_router())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/_apx/eval/label-start",
            json={"judge_name": "quality"},
        )

    assert resp.status_code == 503


# ── label-align route ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_align_runs_memalign_from_labeled_traces(monkeypatch) -> None:
    # New impl: no run_id needed — reads all labeled traces from MLflow directly.
    import types

    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-99")

    import pandas as pd
    fake_trace = types.SimpleNamespace(info=types.SimpleNamespace(trace_id="tr-1"))
    fake_df = pd.DataFrame([{
        "trace_id": "tr-1",
        "request": '{"input":[{"role":"user","content":"test q"}]}',
        "assessments": [{"assessment_name": "quality", "feedback": {"value": True}, "rationale": "good"}],
    }])

    aligned_obj = types.SimpleNamespace(
        instructions="aligned",
        _semantic_memory=[types.SimpleNamespace(guideline_text="Be grounded.")],
    )

    with patch("mlflow.set_tracking_uri"), \
         patch("mlflow.search_traces", return_value=fake_df), \
         patch("mlflow.get_trace", return_value=fake_trace), \
         patch("mlflow.genai.judges.make_judge") as mock_judge, \
         patch("mlflow.genai.judges.optimizers.MemAlignOptimizer") as mock_opt:
        mock_judge.return_value.align.return_value = aligned_obj
        mock_judge.return_value.is_session_level_scorer = False
        mock_opt.return_value = "OPT"

        app = FastAPI()
        app.state.agent_context = _make_ctx()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/_apx/eval/label-align", json={"judge_name": "quality"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["guidelines"] == ["Be grounded."]
    assert body["trace_count"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_align_requires_judge_name(monkeypatch) -> None:
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-99")
    app = FastAPI()
    app.state.agent_context = _make_ctx()
    app.include_router(build_dev_ui_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/_apx/eval/label-align", json={})
    assert resp.status_code == 422
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_align_returns_422_when_no_labeled_traces(monkeypatch) -> None:
    import pandas as pd

    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-99")
    empty_df = pd.DataFrame([{"trace_id": "tr-1", "request": "{}", "assessments": []}])

    with patch("mlflow.set_tracking_uri"), \
         patch("mlflow.search_traces", return_value=empty_df):
        app = FastAPI()
        app.state.agent_context = _make_ctx()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/_apx/eval/label-align", json={"judge_name": "quality"})

    assert resp.status_code == 422
    assert "No traces" in resp.json()["error"]
