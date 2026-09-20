"""Tests for the dev UI feedback widget and judge-alignment routes.

Covers:
- POST /_apx/feedback  (submit rating)
- GET  /_apx/feedback/{trace_id}  (read back — button-state persistence)
- POST /_apx/eval/label-start
- POST /_apx/eval/label-align
- GET  /_apx/eval/label-history
"""

from __future__ import annotations

import os
from types import SimpleNamespace
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
         patch("mlflow.genai.judges.optimizers.MemAlignOptimizer") as mock_opt, \
         patch("apx_agent._labeling.log_align_run", return_value="align-1"):
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


class _FakeAlignRun:
    def __init__(self, run_id: str) -> None:
        self.info = SimpleNamespace(run_id=run_id)

    def __enter__(self) -> "_FakeAlignRun":
        return self

    def __exit__(self, *args) -> None:
        return None


class _FakeMLflow:
    """In-memory MLflow surface for write-then-read MemAlign history tests."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self._seq = 0
        self.set_tracking_uri_calls: list = []
        self.last_filter: str | None = None

    def set_tracking_uri(self, uri) -> None:
        self.set_tracking_uri_calls.append(uri)

    def active_run(self) -> None:
        return None

    def start_run(self, **kwargs) -> _FakeAlignRun:
        self._seq += 1
        run_id = f"align-{self._seq}"
        self.runs.append({
            "run_id": run_id,
            "start_time": 1_700_000_000_000 - self._seq,
            "experiment_id": kwargs.get("experiment_id"),
            "params": {},
            "tags": {},
            "artifact": None,
        })
        return _FakeAlignRun(run_id)

    def set_tag(self, key: str, value: str) -> None:
        self.runs[-1]["tags"][key] = value

    def log_params(self, params: dict[str, str]) -> None:
        self.runs[-1]["params"].update(params)

    def log_dict(self, payload: dict, path: str) -> None:
        self.runs[-1]["artifact"] = {"path": path, "payload": payload}

    @property
    def artifacts(self):
        store = self

        class _Arts:
            def load_dict(self, uri: str):
                # uri looks like runs:/align-1/guidelines.json
                run_id = uri.split("/")[1] if uri.startswith("runs:/") else ""
                for run in store.runs:
                    if run["run_id"] == run_id and run["artifact"] is not None:
                        return run["artifact"]["payload"]
                raise FileNotFoundError(uri)

        return _Arts()

    def search_runs(self, **kwargs):
        import pandas as pd

        self.last_filter = kwargs.get("filter_string")
        rows = []
        for run in self.runs:
            row = {
                "run_id": run["run_id"],
                "start_time": run["start_time"],
                "params.judge_name": run["params"].get("judge_name"),
                "params.registered_as": run["params"].get("registered_as"),
                "params.trace_count": run["params"].get("trace_count"),
                "params.guideline_count": run["params"].get("guideline_count"),
                "params.guidelines_json": run["params"].get("guidelines_json"),
                "tags.apx.kind": run["tags"].get("apx.kind"),
            }
            rows.append(row)
        # Newest first — matches the order_by the helper requests.
        rows.sort(key=lambda rec: rec["start_time"] or 0, reverse=True)
        return pd.DataFrame(rows)


def _patch_mlflow_history(fake: _FakeMLflow):
    return patch.multiple(
        "mlflow",
        set_tracking_uri=fake.set_tracking_uri,
        start_run=fake.start_run,
        set_tag=fake.set_tag,
        log_params=fake.log_params,
        log_dict=fake.log_dict,
        search_runs=fake.search_runs,
        active_run=fake.active_run,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_align_persists_run_and_history_reads_it_back(monkeypatch) -> None:
    """POST /label-align must log an apx.kind=memalign run that GET /label-history returns."""
    import pandas as pd
    from apx_agent import _labeling

    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-99")
    fake = _FakeMLflow()
    fake_trace = SimpleNamespace(info=SimpleNamespace(trace_id="tr-1"))
    fake_df = pd.DataFrame([{
        "trace_id": "tr-1",
        "request": '{"input":[{"role":"user","content":"test q"}]}',
        "assessments": [{"assessment_name": "quality", "feedback": {"value": True}, "rationale": "good"}],
    }])
    aligned_obj = SimpleNamespace(
        instructions="aligned",
        _semantic_memory=[SimpleNamespace(guideline_text="Be grounded.")],
    )

    with patch("mlflow.search_traces", return_value=fake_df),          patch("mlflow.get_trace", return_value=fake_trace),          patch("mlflow.genai.judges.make_judge") as mock_judge,          patch("mlflow.genai.judges.optimizers.MemAlignOptimizer") as mock_opt,          _patch_mlflow_history(fake):
        mock_judge.return_value.align.return_value = aligned_obj
        mock_judge.return_value.is_session_level_scorer = False
        mock_opt.return_value = "OPT"

        app = FastAPI()
        app.state.agent_context = _make_ctx()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            post = await client.post("/_apx/eval/label-align", json={"judge_name": "quality"})
            history = await client.get("/_apx/eval/label-history", params={"judge_name": "quality"})

    assert post.status_code == 200
    body = post.json()
    assert body["ok"] is True
    assert body["guidelines"] == ["Be grounded."]
    assert body["run_id"] == "align-1"

    assert fake.runs, "successful align must open an MLflow run"
    logged = fake.runs[0]
    assert logged["tags"]["apx.kind"] == _labeling.ALIGN_KIND_VALUE
    assert logged["params"]["judge_name"] == "quality"
    assert logged["params"]["trace_count"] == "1"
    assert logged["params"]["guidelines_json"] == '["Be grounded."]'
    assert logged["artifact"]["path"] == _labeling.ALIGN_GUIDELINES_ARTIFACT
    assert logged["artifact"]["payload"] == {"guidelines": ["Be grounded."]}

    assert history.status_code == 200
    listed = history.json()
    assert listed["ok"] is True
    assert fake.last_filter is not None
    assert 'tags.apx.kind = "memalign"' in fake.last_filter
    assert 'params.judge_name = "quality"' in fake.last_filter
    assert len(listed["runs"]) == 1
    assert listed["runs"][0]["run_id"] == "align-1"
    assert listed["runs"][0]["guidelines"] == ["Be grounded."]
    assert listed["runs"][0]["judge_name"] == "quality"
    assert listed["runs"][0]["trace_count"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_history_empty_experiment_returns_empty_list(monkeypatch) -> None:
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-99")
    fake = _FakeMLflow()
    with _patch_mlflow_history(fake):
        app = FastAPI()
        app.state.agent_context = _make_ctx()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/_apx/eval/label-history")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["runs"] == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_history_returns_503_without_experiment_id(monkeypatch) -> None:
    monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    app = FastAPI()
    app.state.agent_context = _make_ctx()
    app.include_router(build_dev_ui_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/_apx/eval/label-history")
    assert resp.status_code == 503
    assert resp.json()["ok"] is False


@pytest.mark.unit
def test_eval_landing_includes_alignment_timeline() -> None:
    from apx_agent._ui_chat import _render_eval_landing

    html = _render_eval_landing([], None, None)
    assert 'id="la-history"' in html
    assert "/_apx/eval/label-history" in html
    assert "Judge aligned" in html
    assert "No alignments yet." in html
    assert "loadAlignHistory()" in html
