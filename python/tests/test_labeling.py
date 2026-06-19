import pytest
import pandas as pd
from datetime import datetime, timezone
from types import SimpleNamespace
from mlflow.genai import label_schemas as ls
from apx_agent import _labeling


def _judge(name="domain_quality_base", ft=float, instr="rate {{ inputs }} {{ outputs }}"):
    return SimpleNamespace(name=name, instructions=instr, feedback_value_type=ft)


@pytest.mark.unit
def test_parse_scale_ok():
    assert _labeling.parse_scale("1-5") == (1.0, 5.0)
    assert _labeling.parse_scale("0.0 - 1.0") == (0.0, 1.0)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "5", "a-b", "1-2-3"])
def test_parse_scale_bad(bad):
    with pytest.raises(_labeling.LabelingError):
        _labeling.parse_scale(bad)


@pytest.mark.unit
def test_derive_numeric_schema_matches_judge():
    spec = _labeling.derive_label_schema(judge=_judge(ft=float), scale="1-5", options=None)
    assert spec["name"] == "domain_quality_base"          # verbatim from judge
    assert spec["instruction"] == "rate {{ inputs }} {{ outputs }}"
    assert spec["type"] == "feedback"
    assert spec["enable_comment"] is True
    assert spec["overwrite"] is True
    assert isinstance(spec["input"], ls.InputNumeric)
    assert (spec["input"].min_value, spec["input"].max_value) == (1.0, 5.0)


@pytest.mark.unit
def test_derive_numeric_requires_scale():
    with pytest.raises(_labeling.LabelingError, match="--scale"):
        _labeling.derive_label_schema(judge=_judge(ft=float), scale=None, options=None)


@pytest.mark.unit
def test_derive_bool_maps_to_categorical_true_false():
    spec = _labeling.derive_label_schema(judge=_judge(ft=bool), scale=None, options=None)
    assert isinstance(spec["input"], ls.InputCategorical)
    assert spec["input"].options == ["true", "false"]


@pytest.mark.unit
def test_derive_str_requires_options():
    with pytest.raises(_labeling.LabelingError, match="--options"):
        _labeling.derive_label_schema(judge=_judge(ft=str), scale=None, options=None)
    spec = _labeling.derive_label_schema(judge=_judge(ft=str), scale=None, options=["good", "bad"])
    assert isinstance(spec["input"], ls.InputCategorical)
    assert spec["input"].options == ["good", "bad"]


@pytest.mark.unit
def test_make_run_id_is_deterministic():
    now = datetime(2026, 6, 17, 19, 5, 30, tzinfo=timezone.utc)
    assert _labeling.make_run_id("domain_quality_base", now) == "domain_quality_base-20260617T190530Z"


@pytest.mark.unit
def test_name_helpers():
    rid = "domain_quality_base-20260617T190530Z"
    assert _labeling.session_name_for(rid) == f"{rid}_sme"
    assert _labeling.RUN_TAG == "apx.label.run"


@pytest.mark.unit
def test_resolve_experiment_prefers_explicit():
    eid = _labeling.resolve_experiment_id(
        explicit="123", agent_tags={"apx.mlflow.experiment_id": "999"})
    assert eid == "123"


@pytest.mark.unit
def test_resolve_experiment_falls_back_to_tag():
    eid = _labeling.resolve_experiment_id(
        explicit=None, agent_tags={"apx.mlflow.experiment_id": "999"})
    assert eid == "999"
    assert _labeling.EXPERIMENT_TAG == "apx.mlflow.experiment_id"


@pytest.mark.unit
def test_resolve_experiment_raises_when_unresolved():
    with pytest.raises(_labeling.LabelingError, match="--experiment"):
        _labeling.resolve_experiment_id(explicit=None, agent_tags={})


@pytest.mark.unit
def test_resolve_experiment_empty_string_falls_through_and_raises():
    with pytest.raises(_labeling.LabelingError, match="--experiment"):
        _labeling.resolve_experiment_id(explicit="", agent_tags={})


@pytest.mark.unit
def test_select_scored_traces_returns_df(monkeypatch):
    df = pd.DataFrame({"trace_id": ["t1", "t2"]})
    monkeypatch.setattr(_labeling, "search_traces_for_experiment", lambda exp, **kw: df)
    out = _labeling.select_scored_traces(
        experiment_id="123", judge_name="j", filter_string=None, limit=None)
    assert list(out["trace_id"]) == ["t1", "t2"]


@pytest.mark.unit
def test_select_scored_traces_passes_include_spans_false(monkeypatch):
    # FEVM/private-link workspaces block the trace blob store; a span read makes
    # search_traces silently return 0 rows. The read must be metadata-only.
    captured: dict = {}

    def fake(exp, **kw):
        captured.update(kw)
        return pd.DataFrame({"trace_id": ["t1"]})

    monkeypatch.setattr(_labeling, "search_traces_for_experiment", fake)
    _labeling.select_scored_traces(
        experiment_id="123", judge_name="j", filter_string=None, limit=None)
    assert captured.get("include_spans") is False


@pytest.mark.unit
def test_select_scored_traces_empty_fails_fast(monkeypatch):
    monkeypatch.setattr(_labeling, "search_traces_for_experiment",
                        lambda exp, **kw: pd.DataFrame({"trace_id": []}))
    with pytest.raises(_labeling.LabelingError, match="--evaluate"):
        _labeling.select_scored_traces(
            experiment_id="123", judge_name="j", filter_string=None, limit=None)
    # Error message must NOT mention --since (label start has no --since option)
    try:
        _labeling.select_scored_traces(
            experiment_id="123", judge_name="j", filter_string=None, limit=None)
    except _labeling.LabelingError as exc:
        assert "--since" not in str(exc)


@pytest.mark.unit
def test_select_scored_traces_judge_score_present_returns_df(monkeypatch):
    """Happy path: at least one trace carries the judge's assessment."""
    # Mirroring real Assessment.to_dictionary() output (assessment_name key, proto field names).
    scored_assessment = {
        "assessment_name": "domain_quality",
        "trace_id": "t1",
        "source": {"source_type": "LLM_JUDGE", "source_id": "domain_quality"},
        "feedback": {"value": 0.9},
    }
    df = pd.DataFrame({
        "trace_id": ["t1", "t2"],
        "assessments": [
            [scored_assessment],  # t1 has the judge's score
            [],                   # t2 is unscored — that's fine, at least one suffices
        ],
    })
    monkeypatch.setattr(_labeling, "search_traces_for_experiment", lambda exp, **kw: df)
    out = _labeling.select_scored_traces(
        experiment_id="123", judge_name="domain_quality", filter_string=None, limit=None)
    assert list(out["trace_id"]) == ["t1", "t2"]


@pytest.mark.unit
def test_select_scored_traces_no_judge_score_raises(monkeypatch):
    """Reject: non-empty traces but none carry the judge's assessment."""
    other_assessment = {
        "assessment_name": "other_judge",
        "trace_id": "t1",
        "source": {"source_type": "LLM_JUDGE", "source_id": "other_judge"},
        "feedback": {"value": 0.5},
    }
    df = pd.DataFrame({
        "trace_id": ["t1", "t2"],
        "assessments": [
            [other_assessment],  # t1 has a score but for the WRONG judge
            [],                  # t2 is unscored
        ],
    })
    monkeypatch.setattr(_labeling, "search_traces_for_experiment", lambda exp, **kw: df)
    with pytest.raises(_labeling.LabelingError, match="score them first"):
        _labeling.select_scored_traces(
            experiment_id="123", judge_name="domain_quality", filter_string=None, limit=None)


@pytest.mark.unit
def test_tag_traces_sets_run_tag(monkeypatch):
    calls = []
    monkeypatch.setattr(_labeling, "set_trace_tag",
                        lambda **kw: calls.append(kw))
    n = _labeling.tag_traces(["t1", "t2"], "run-1")
    assert n == 2
    assert all(c["key"] == _labeling.RUN_TAG and c["value"] == "run-1" for c in calls)
    assert {c["trace_id"] for c in calls} == {"t1", "t2"}


@pytest.mark.unit
def test_start_session_creates_schema_with_judge_name(monkeypatch):
    judge = _judge(name="domain_quality_base", ft=float)
    monkeypatch.setattr(_labeling, "set_experiment", lambda **kw: None)
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: judge)

    created = {}
    monkeypatch.setattr(_labeling, "create_label_schema",
                        lambda **kw: created.update(kw))
    monkeypatch.setattr(_labeling, "select_scored_traces",
                        lambda **kw: pd.DataFrame({"trace_id": ["t1", "t2"]}))
    monkeypatch.setattr(_labeling, "tag_traces", lambda ids, rid: len(ids))

    added = {}
    session = SimpleNamespace(add_traces=lambda traces: (added.update(n=len(traces)), session)[1],
                              url="https://x/sme")
    sess_kwargs = {}
    monkeypatch.setattr(_labeling, "create_labeling_session",
                        lambda **kw: (sess_kwargs.update(kw), session)[1])
    monkeypatch.setattr(_labeling, "get_review_app", lambda experiment_id: None)

    res = _labeling.start_session(
        experiment_id="123", agent_name="payroll", judge_name="domain_quality_base",
        scale="1-5", options=None, assignees=["sme@x.com"], filter_string=None,
        limit=None, endpoint=None, attach_agent=False,
        now=datetime(2026, 6, 17, 19, 5, 30, tzinfo=timezone.utc),
    )
    # schema name MUST equal judge name; session references that schema name
    assert created["name"] == "domain_quality_base"
    assert sess_kwargs["label_schemas"] == ["domain_quality_base"]
    assert res.run_id == "domain_quality_base-20260617T190530Z"
    assert res.session_url == "https://x/sme"
    assert res.trace_count == 2
    assert added["n"] == 2, "scored traces are added to the session via add_traces"


def _base_start_session_monkeypatches(monkeypatch):
    """Shared stubs for start_session isolation tests."""
    judge = _judge(name="domain_quality_base", ft=float)
    monkeypatch.setattr(_labeling, "set_experiment", lambda **kw: None)
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: judge)
    monkeypatch.setattr(_labeling, "create_label_schema", lambda **kw: None)
    monkeypatch.setattr(_labeling, "select_scored_traces",
                        lambda **kw: pd.DataFrame({"trace_id": ["t1", "t2"]}))
    monkeypatch.setattr(_labeling, "tag_traces", lambda ids, rid: len(ids))

    session = SimpleNamespace(add_traces=lambda traces: session, url="https://x/sme")
    monkeypatch.setattr(_labeling, "create_labeling_session", lambda **kw: session)
    return session


@pytest.mark.unit
def test_start_session_attaches_agent_when_requested(monkeypatch):
    ds = _base_start_session_monkeypatches(monkeypatch)
    add_agent_calls = []
    review_app = SimpleNamespace(add_agent=lambda **kw: add_agent_calls.append(kw))
    monkeypatch.setattr(_labeling, "get_review_app", lambda experiment_id: review_app)

    _labeling.start_session(
        experiment_id="123", agent_name="payroll", judge_name="domain_quality_base",
        scale="1-5", options=None, assignees=["sme@x.com"], filter_string=None,
        limit=None, endpoint="https://ep", attach_agent=True,
        now=datetime(2026, 6, 17, 19, 5, 30, tzinfo=timezone.utc),
    )
    assert len(add_agent_calls) == 1
    assert add_agent_calls[0]["agent_name"] == "payroll"
    assert add_agent_calls[0]["model_serving_endpoint"] == "https://ep"
    assert add_agent_calls[0]["overwrite"] is True


@pytest.mark.unit
def test_start_session_raises_when_review_app_missing(monkeypatch):
    _base_start_session_monkeypatches(monkeypatch)
    monkeypatch.setattr(_labeling, "get_review_app", lambda experiment_id: None)

    with pytest.raises(_labeling.LabelingError, match="no review app found"):
        _labeling.start_session(
            experiment_id="123", agent_name="payroll", judge_name="domain_quality_base",
            scale="1-5", options=None, assignees=["sme@x.com"], filter_string=None,
            limit=None, endpoint="https://ep", attach_agent=True,
            now=datetime(2026, 6, 17, 19, 5, 30, tzinfo=timezone.utc),
        )


@pytest.mark.unit
def test_align_judge_missing_dspy_raises_friendly(monkeypatch):
    # Force the MemAlign import to fail like a missing dspy.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("mlflow.genai.judges.optimizers"):
            raise ImportError("DSPy library is required but not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(_labeling.LabelingError, match=r"apx-agent\[align\]"):
        _labeling.align_judge(
            experiment_id="123", judge_name="j", run_id="r1",
            reflection_model="databricks:/databricks-claude-sonnet-4-6",
            embedding_model="databricks:/databricks-gte-large-en",
            retrieval_k=5, new_version=None,
        )


@pytest.mark.unit
def test_align_judge_missing_dspy_mlflowexception_raises_friendly(monkeypatch):
    # On real environments without dspy, the optimizers import raises MlflowException,
    # not ImportError. The guard must convert it to the install-hint LabelingError.
    import builtins
    from mlflow.exceptions import MlflowException
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("mlflow.genai.judges.optimizers"):
            raise MlflowException("DSPy library is required but not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(_labeling.LabelingError, match=r"apx-agent\[align\]"):
        _labeling.align_judge(
            experiment_id="123", judge_name="j", run_id="r1",
            reflection_model="databricks:/databricks-claude-sonnet-4-6",
            embedding_model="databricks:/databricks-gte-large-en",
            retrieval_k=5, new_version=None,
        )


@pytest.mark.unit
def test_align_judge_aligns_and_updates_in_place(monkeypatch):
    captured = {}

    aligned = SimpleNamespace(
        instructions="distilled...",
        _semantic_memory=[SimpleNamespace(guideline_text="be precise")],
        update=lambda **kw: captured.update(kw) or SimpleNamespace(name="j"),
    )
    base = SimpleNamespace(align=lambda **kw: (captured.update(align=kw), aligned)[1])
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: base)
    monkeypatch.setattr(_labeling, "_load_memalign",
                        lambda **kw: "OPT")  # bypass dspy import
    search_kw: dict = {}

    def fake_search(exp, **kw):
        search_kw.update(kw)
        return ["trace-a", "trace-b"]

    monkeypatch.setattr(_labeling, "search_traces_for_experiment", fake_search)

    res = _labeling.align_judge(
        experiment_id="123", judge_name="j", run_id="r1",
        reflection_model="databricks:/m", embedding_model="databricks:/e",
        retrieval_k=5, new_version=None,
    )
    assert res.guidelines == ["be precise"]
    assert captured["align"]["optimizer"] == "OPT"
    assert captured["align"]["traces"] == ["trace-a", "trace-b"]
    assert "experiment_id" in captured  # update() was called in-place
    # FEVM footgun: the run-tagged trace read must be metadata-only too.
    assert search_kw.get("include_spans") is False


@pytest.mark.unit
def test_align_judge_no_sme_labels_raises_friendly(monkeypatch):
    # MemAlign raises MlflowException "No valid feedback records found" when the
    # run's traces have no human/SME labels yet; align must turn that into a
    # clear LabelingError pointing the user back to the Review App.
    from mlflow.exceptions import MlflowException

    def boom(**kw):
        raise MlflowException(
            "Alignment optimization failed: No valid feedback records found in traces.")

    base = SimpleNamespace(align=boom)
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: base)
    monkeypatch.setattr(_labeling, "_load_memalign", lambda **kw: "OPT")
    monkeypatch.setattr(_labeling, "search_traces_for_experiment",
                        lambda exp, **kw: ["trace-a"])

    with pytest.raises(_labeling.LabelingError, match="no SME labels"):
        _labeling.align_judge(
            experiment_id="123", judge_name="j", run_id="r1",
            reflection_model="databricks:/m", embedding_model="databricks:/e",
            retrieval_k=5, new_version=None,
        )


@pytest.mark.unit
def test_align_judge_new_version_makes_and_registers(monkeypatch):
    make_judge_calls = {}
    register_calls = {}

    def fake_make_judge(**kw):
        make_judge_calls.update(kw)
        judge_ns = SimpleNamespace(register=lambda **rkw: register_calls.update(rkw))
        return judge_ns

    aligned = SimpleNamespace(
        instructions="distilled v2...",
        _semantic_memory=[SimpleNamespace(guideline_text="be very precise")],
        update=lambda **kw: SimpleNamespace(name="j"),
    )
    base = SimpleNamespace(
        feedback_value_type=float,
        model="databricks:/model",
        align=lambda **kw: aligned,
    )
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: base)
    monkeypatch.setattr(_labeling, "_load_memalign", lambda **kw: "OPT")
    monkeypatch.setattr(_labeling, "search_traces_for_experiment",
                        lambda exp, **kw: ["trace-a", "trace-b"])
    monkeypatch.setattr("mlflow.genai.judges.make_judge", fake_make_judge)

    res = _labeling.align_judge(
        experiment_id="exp-42", judge_name="j", run_id="r1",
        reflection_model="databricks:/m", embedding_model="databricks:/e",
        retrieval_k=5, new_version="v2",
    )

    assert res.registered_as == "v2"
    assert make_judge_calls["name"] == "v2"
    assert make_judge_calls["feedback_value_type"] is float
    assert make_judge_calls["model"] == "databricks:/model"
    assert register_calls["experiment_id"] == "exp-42"
    assert res.guidelines == ["be very precise"]


@pytest.mark.unit
def test_set_uc_tags_includes_experiment_id():
    from apx_agent import _watchdog

    class FakeClient:
        def __init__(self):
            self.tags = {}
        def set_registered_model_tag(self, *, name, key, value):
            self.tags[key] = value

    class FakeAgent:
        _name = "payroll"

    client = FakeClient()
    # emit_agent_metadata needs a real-ish agent; patch it to a minimal dict.
    import apx_agent._watchdog as w
    orig = w.emit_agent_metadata
    w.emit_agent_metadata = lambda agent, name=None, model=None: {"name": "payroll", "model": "m"}
    try:
        _watchdog.set_uc_tags_for_agent(
            FakeAgent(), registered_model_name="c.s.payroll",
            experiment_id="555", mlflow_client=client,
        )
    finally:
        w.emit_agent_metadata = orig
    assert client.tags.get("apx.mlflow.experiment_id") == "555"
