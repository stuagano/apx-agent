import logging
import pytest
import pandas as pd
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from mlflow.genai import label_schemas as ls
from apx_agent import _labeling

def _human_assessment(name: str, value: Any, rationale: str = "labeled by reviewer") -> dict:
    return {
        "assessment_name": name,
        "source": {"source_type": "HUMAN", "source_id": "reviewer"},
        "feedback": {"value": value},
        "rationale": rationale,
    }


def _align_trace(trace_id: str, value: Any, *, name: str = "j") -> SimpleNamespace:
    return SimpleNamespace(
        info=SimpleNamespace(
            trace_id=trace_id,
            assessments=[_human_assessment(name, value)],
        )
    )




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
    import apx_agent._labeling as _lab_mod
    monkeypatch.setattr(_lab_mod, "_mlflow",
                        SimpleNamespace(get_trace=lambda tid: SimpleNamespace(info=SimpleNamespace(trace_id=tid))))

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
    assert added.get("n", 0) == 2, "scored traces are added to the session via add_traces"


@pytest.mark.unit
def test_start_session_skipped_trace_is_logged_and_counted(monkeypatch, caplog):
    # #763: a trace whose spans can't be fetched (e.g. FEVM blob store) must be
    # logged and excluded from the reported count — never silently dropped.
    judge = _judge(name="dq", ft=float)
    monkeypatch.setattr(_labeling, "set_experiment", lambda **kw: None)
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: judge)
    monkeypatch.setattr(_labeling, "create_label_schema", lambda **kw: None)
    monkeypatch.setattr(_labeling, "select_scored_traces",
                        lambda **kw: pd.DataFrame({"trace_id": ["t1", "t2"]}))
    monkeypatch.setattr(_labeling, "tag_traces", lambda ids, rid: len(ids))

    def flaky_get_trace(tid):
        if tid == "t2":
            raise RuntimeError("span blob store blocked")
        return SimpleNamespace(info=SimpleNamespace(trace_id=tid))

    import apx_agent._labeling as _lab_mod
    monkeypatch.setattr(_lab_mod, "_mlflow", SimpleNamespace(get_trace=flaky_get_trace))

    added = {}
    session = SimpleNamespace(
        add_traces=lambda traces: (added.update(n=len(traces)), session)[1],
        url="https://x/sme")
    monkeypatch.setattr(_labeling, "create_labeling_session", lambda **kw: session)
    monkeypatch.setattr(_labeling, "get_review_app", lambda experiment_id: None)

    with caplog.at_level(logging.WARNING):
        res = _labeling.start_session(
            experiment_id="123", agent_name="a", judge_name="dq",
            scale="1-5", options=None, assignees=[], filter_string=None,
            limit=None, endpoint=None, attach_agent=False,
            now=datetime(2026, 6, 17, 19, 5, 30, tzinfo=timezone.utc),
        )

    assert added["n"] == 1, "only the fetchable trace is added to the session"
    assert res.trace_count == 1, "count reflects traces actually added, not matched"
    assert any("t2" in r.message for r in caplog.records), "skipped trace is logged, not swallowed"


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
    base = SimpleNamespace(
        is_session_level_scorer=False,
        align=lambda **kw: (captured.update(align=kw), aligned)[1],
    )
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: base)
    monkeypatch.setattr(_labeling, "_load_memalign",
                        lambda **kw: "OPT")  # bypass dspy import
    search_kw: dict = {}

    trace_a = _align_trace("trace-a", True)
    trace_b = _align_trace("trace-b", False)

    def fake_search(exp, **kw):
        search_kw.update(kw)
        return [trace_a, trace_b]

    monkeypatch.setattr(_labeling, "search_traces_for_experiment", fake_search)
    # mlflow.get_trace: return the thin traces with HUMAN labels the cohort gate needs.
    fetched = {"trace-a": trace_a, "trace-b": trace_b}
    monkeypatch.setattr(_labeling._mlflow, "get_trace", lambda tid: fetched[tid])

    logged: dict = {}

    def fake_log(**kw):
        logged.update(kw)
        return "align-run-1"

    monkeypatch.setattr(_labeling, "log_align_run", fake_log)

    res = _labeling.align_judge(
        experiment_id="123", judge_name="j", run_id="r1",
        reflection_model="databricks:/m", embedding_model="databricks:/e",
        retrieval_k=5, new_version=None,
    )
    assert res.guidelines == ["be precise"]
    assert res.run_id == "align-run-1"
    assert logged["experiment_id"] == "123"
    assert logged["judge_name"] == "j"
    assert logged["guidelines"] == ["be precise"]
    assert logged["trace_count"] == 2
    assert captured["align"]["optimizer"] == "OPT"
    assert len(captured["align"]["traces"]) == 2
    assert "experiment_id" in captured  # update() was called in-place
    # FEVM footgun: the run-tagged trace read must be metadata-only too.
    assert search_kw.get("include_spans") is False


@pytest.mark.unit
def test_align_judge_no_sme_labels_raises_friendly(monkeypatch):
    # Unlabeled traces now fail in select_alignment_cohort before MemAlign.
    monkeypatch.setattr(_labeling, "_load_memalign", lambda **kw: "OPT")
    thin = [SimpleNamespace(info=SimpleNamespace(trace_id="trace-a", assessments=[]))]
    monkeypatch.setattr(_labeling, "search_traces_for_experiment", lambda exp, **kw: thin)
    monkeypatch.setattr(
        _labeling._mlflow,
        "get_trace",
        lambda tid: SimpleNamespace(info=SimpleNamespace(trace_id=tid, assessments=[])),
    )

    with pytest.raises(_labeling.LabelingError, match="no HUMAN rationale/labels"):
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
        is_session_level_scorer=False,
        feedback_value_type=float,
        model="databricks:/model",
        align=lambda **kw: aligned,
    )
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: base)
    monkeypatch.setattr(_labeling, "_load_memalign", lambda **kw: "OPT")
    thin = [_align_trace("trace-a", True), _align_trace("trace-b", False)]
    monkeypatch.setattr(_labeling, "search_traces_for_experiment", lambda exp, **kw: thin)
    fetched = {"trace-a": thin[0], "trace-b": thin[1]}
    monkeypatch.setattr(_labeling._mlflow, "get_trace", lambda tid: fetched[tid])
    monkeypatch.setattr("mlflow.genai.judges.make_judge", fake_make_judge)
    monkeypatch.setattr(_labeling, "log_align_run", lambda **kw: "align-v2")

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
def test_select_alignment_cohort_keeps_matching_human_pair():
    traces = [_align_trace("t1", True, name="domain_quality"),
              _align_trace("t2", False, name="domain_quality")]
    kept = _labeling.select_alignment_cohort(traces, judge_name="domain_quality")
    assert [t.info.trace_id for t in kept] == ["t1", "t2"]


@pytest.mark.unit
def test_select_alignment_cohort_name_mismatch_raises():
    traces = [
        SimpleNamespace(info=SimpleNamespace(
            trace_id="t1",
            assessments=[
                _human_assessment("other_judge", True),
                {
                    "assessment_name": "domain_quality",
                    "source": {"source_type": "LLM_JUDGE", "source_id": "domain_quality"},
                    "feedback": {"value": True},
                    "rationale": "model score",
                },
            ],
        )),
    ]
    with pytest.raises(_labeling.LabelingError, match=r"differs from judge"):
        _labeling.select_alignment_cohort(traces, judge_name="domain_quality")


@pytest.mark.unit
def test_select_alignment_cohort_missing_rationale_raises():
    traces = [
        SimpleNamespace(info=SimpleNamespace(
            trace_id="t1",
            assessments=[_human_assessment("domain_quality", True, rationale="   ")],
        )),
        SimpleNamespace(info=SimpleNamespace(
            trace_id="t2",
            assessments=[{
                "assessment_name": "domain_quality",
                "source": {"source_type": "HUMAN", "source_id": "reviewer"},
                "feedback": {"value": False},
            }],
        )),
    ]
    with pytest.raises(_labeling.LabelingError, match="no HUMAN rationale/labels"):
        _labeling.select_alignment_cohort(traces, judge_name="domain_quality")


@pytest.mark.unit
def test_select_alignment_cohort_one_sided_labels_raises():
    traces = [
        _align_trace("t1", True, name="domain_quality"),
        _align_trace("t2", True, name="domain_quality"),
    ]
    with pytest.raises(_labeling.LabelingError, match="diversity"):
        _labeling.select_alignment_cohort(traces, judge_name="domain_quality")


@pytest.mark.unit
def test_select_alignment_cohort_llm_judge_only_raises():
    traces = [
        SimpleNamespace(info=SimpleNamespace(
            trace_id="t1",
            assessments=[{
                "assessment_name": "domain_quality",
                "source": {"source_type": "LLM_JUDGE", "source_id": "domain_quality"},
                "feedback": {"value": True},
                "rationale": "model score",
            }],
        )),
    ]
    with pytest.raises(_labeling.LabelingError, match="no HUMAN rationale/labels"):
        _labeling.select_alignment_cohort(traces, judge_name="domain_quality")


@pytest.mark.unit
def test_align_judge_no_feedback_records_still_remaps(monkeypatch):
    from mlflow.exceptions import MlflowException

    def boom(**kw):
        raise MlflowException(
            "Alignment optimization failed: No valid feedback records found in traces.")

    base = SimpleNamespace(is_session_level_scorer=False, align=boom)
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: base)
    monkeypatch.setattr(_labeling, "_load_memalign", lambda **kw: "OPT")
    thin = [_align_trace("trace-a", True), _align_trace("trace-b", False)]
    monkeypatch.setattr(_labeling, "search_traces_for_experiment", lambda exp, **kw: thin)
    fetched = {"trace-a": thin[0], "trace-b": thin[1]}
    monkeypatch.setattr(_labeling._mlflow, "get_trace", lambda tid: fetched[tid])
    monkeypatch.setattr(_labeling, "log_align_run", lambda **kw: None)

    with pytest.raises(_labeling.LabelingError, match="no SME labels"):
        _labeling.align_judge(
            experiment_id="123", judge_name="j", run_id="r1",
            reflection_model="databricks:/m", embedding_model="databricks:/e",
            retrieval_k=5, new_version=None,
        )

@pytest.mark.unit
def test_set_uc_tags_includes_experiment_id():
    from apx_agent import _governance

    class FakeClient:
        def __init__(self):
            self.tags = {}
        def set_registered_model_tag(self, *, name, key, value):
            self.tags[key] = value

    class FakeAgent:
        _name = "payroll"

    client = FakeClient()
    # emit_agent_metadata needs a real-ish agent; patch it to a minimal dict.
    import apx_agent._governance as w
    orig = w.emit_agent_metadata
    w.emit_agent_metadata = lambda agent, name=None, model=None: {"name": "payroll", "model": "m"}
    try:
        _governance.set_uc_tags_for_agent(
            FakeAgent(), registered_model_name="c.s.payroll",
            experiment_id="555", mlflow_client=client,
        )
    finally:
        w.emit_agent_metadata = orig
    assert client.tags.get("apx.mlflow.experiment_id") == "555"




class _MemAlignStore:
    """Tiny write/read stand-in so helper tests do not import the route suite."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self._seq = 0
        self.last_filter: str | None = None

    def active_run(self):
        return None

    def start_run(self, **kwargs):
        self._seq += 1
        run_id = f"align-{self._seq}"
        self.runs.append({
            "run_id": run_id,
            "start_time": 1_700_000_000_000 - self._seq,
            "params": {},
            "tags": {},
            "artifact": None,
        })
        store = self

        class _Run:
            info = type("info", (), {"run_id": run_id})()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        return _Run()

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
                run_id = uri.split("/")[1] if uri.startswith("runs:/") else ""
                for run in store.runs:
                    if run["run_id"] == run_id and run["artifact"] is not None:
                        return run["artifact"]["payload"]
                raise FileNotFoundError(uri)

        return _Arts()

    def search_runs(self, **kwargs):
        import pandas as pd

        self.last_filter = kwargs.get("filter_string")
        rows = [{
            "run_id": run["run_id"],
            "start_time": run["start_time"],
            "params.judge_name": run["params"].get("judge_name"),
            "params.registered_as": run["params"].get("registered_as"),
            "params.trace_count": run["params"].get("trace_count"),
            "params.guideline_count": run["params"].get("guideline_count"),
            "params.guidelines_json": run["params"].get("guidelines_json"),
            "tags.apx.kind": run["tags"].get("apx.kind"),
        } for run in self.runs]
        rows.sort(key=lambda rec: rec["start_time"] or 0, reverse=True)
        return pd.DataFrame(rows)

@pytest.mark.unit
def test_log_align_run_is_listed_by_list_align_runs():
    """Write path + search_runs filter must round-trip guidelines (Ctk)."""
    fake = _MemAlignStore()
    run_id = _labeling.log_align_run(
        experiment_id="exp-1",
        judge_name="quality",
        registered_as="quality",
        trace_count=3,
        guidelines=["Be grounded.", "Cite sources."],
        mlflow_api=fake,
    )
    assert run_id == "align-1"
    assert fake.runs[0]["tags"]["apx.kind"] == "memalign"

    listed = _labeling.list_align_runs(
        experiment_id="exp-1",
        judge_name="quality",
        mlflow_api=fake,
    )
    assert fake.last_filter is not None
    assert 'tags.apx.kind = "memalign"' in fake.last_filter
    assert 'params.judge_name = "quality"' in fake.last_filter
    assert len(listed) == 1
    assert listed[0].run_id == "align-1"
    assert listed[0].guidelines == ["Be grounded.", "Cite sources."]
    assert listed[0].trace_count == 3
    assert listed[0].judge_name == "quality"


@pytest.mark.unit
def test_list_align_runs_empty_when_no_memalign_runs():
    fake = _MemAlignStore()
    listed = _labeling.list_align_runs(experiment_id="exp-1", mlflow_api=fake)
    assert listed == []


@pytest.mark.unit
def test_log_align_run_long_guidelines_use_artifact_not_param():
    fake = _MemAlignStore()
    long = ["x" * 200, "y" * 200, "z" * 200]
    _labeling.log_align_run(
        experiment_id="exp-1",
        judge_name="quality",
        registered_as="quality",
        trace_count=1,
        guidelines=long,
        mlflow_api=fake,
    )
    assert "guidelines_json" not in fake.runs[0]["params"]
    assert fake.runs[0]["artifact"]["payload"]["guidelines"] == long
    assert fake.runs[0]["params"]["guideline_count"] == "3"
    listed = _labeling.list_align_runs(experiment_id="exp-1", mlflow_api=fake)
    assert listed[0].guidelines == long
