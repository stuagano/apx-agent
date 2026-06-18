import pytest
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


import pandas as pd


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
