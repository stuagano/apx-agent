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
    assert _labeling.dataset_name_for("payroll", rid) == f"payroll_label_{rid}"
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
def test_select_scored_traces_empty_fails_fast(monkeypatch):
    monkeypatch.setattr(_labeling, "search_traces_for_experiment",
                        lambda exp, **kw: pd.DataFrame({"trace_id": []}))
    with pytest.raises(_labeling.LabelingError, match="--evaluate"):
        _labeling.select_scored_traces(
            experiment_id="123", judge_name="j", filter_string=None, limit=None)


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
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: judge)

    created = {}
    monkeypatch.setattr(_labeling, "create_label_schema",
                        lambda **kw: created.update(kw))
    monkeypatch.setattr(_labeling, "select_scored_traces",
                        lambda **kw: pd.DataFrame({"trace_id": ["t1", "t2"]}))
    monkeypatch.setattr(_labeling, "tag_traces", lambda ids, rid: len(ids))

    ds = SimpleNamespace(merge_records=lambda df: ds)
    monkeypatch.setattr(_labeling, "get_dataset", lambda name: (_ for _ in ()).throw(Exception()))
    monkeypatch.setattr(_labeling, "create_dataset", lambda name: ds)

    session = SimpleNamespace(add_dataset=lambda dataset_name: session,
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


def _base_start_session_monkeypatches(monkeypatch):
    """Shared stubs for start_session isolation tests."""
    judge = _judge(name="domain_quality_base", ft=float)
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: judge)
    monkeypatch.setattr(_labeling, "create_label_schema", lambda **kw: None)
    monkeypatch.setattr(_labeling, "select_scored_traces",
                        lambda **kw: pd.DataFrame({"trace_id": ["t1", "t2"]}))
    monkeypatch.setattr(_labeling, "tag_traces", lambda ids, rid: len(ids))

    ds = SimpleNamespace(merge_records=lambda df: ds)
    monkeypatch.setattr(_labeling, "get_dataset", lambda name: (_ for _ in ()).throw(Exception()))
    monkeypatch.setattr(_labeling, "create_dataset", lambda name: ds)

    session = SimpleNamespace(add_dataset=lambda dataset_name: session, url="https://x/sme")
    monkeypatch.setattr(_labeling, "create_labeling_session", lambda **kw: session)
    return ds


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
def test_start_session_uses_existing_dataset(monkeypatch):
    judge = _judge(name="domain_quality_base", ft=float)
    monkeypatch.setattr(_labeling, "get_scorer", lambda **kw: judge)
    monkeypatch.setattr(_labeling, "create_label_schema", lambda **kw: None)
    monkeypatch.setattr(_labeling, "select_scored_traces",
                        lambda **kw: pd.DataFrame({"trace_id": ["t1", "t2"]}))
    monkeypatch.setattr(_labeling, "tag_traces", lambda ids, rid: len(ids))
    monkeypatch.setattr(_labeling, "get_review_app", lambda experiment_id: None)

    merge_calls = []
    existing_ds = SimpleNamespace(merge_records=lambda df: merge_calls.append(df))
    create_dataset_calls = []
    monkeypatch.setattr(_labeling, "get_dataset", lambda name: existing_ds)
    monkeypatch.setattr(_labeling, "create_dataset",
                        lambda name: create_dataset_calls.append(name) or existing_ds)

    session = SimpleNamespace(add_dataset=lambda dataset_name: session, url="https://x/sme")
    monkeypatch.setattr(_labeling, "create_labeling_session", lambda **kw: session)

    _labeling.start_session(
        experiment_id="123", agent_name="payroll", judge_name="domain_quality_base",
        scale="1-5", options=None, assignees=["sme@x.com"], filter_string=None,
        limit=None, endpoint=None, attach_agent=False,
        now=datetime(2026, 6, 17, 19, 5, 30, tzinfo=timezone.utc),
    )
    assert create_dataset_calls == [], "create_dataset should NOT be called when get_dataset succeeds"
    assert len(merge_calls) == 1, "merge_records should still be called on the existing dataset"


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
    monkeypatch.setattr(_labeling, "search_traces_for_experiment",
                        lambda exp, **kw: ["trace-a", "trace-b"])

    res = _labeling.align_judge(
        experiment_id="123", judge_name="j", run_id="r1",
        reflection_model="databricks:/m", embedding_model="databricks:/e",
        retrieval_k=5, new_version=None,
    )
    assert res.guidelines == ["be precise"]
    assert captured["align"]["optimizer"] == "OPT"
    assert captured["align"]["traces"] == ["trace-a", "trace-b"]
    assert "experiment_id" in captured  # update() was called in-place


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
