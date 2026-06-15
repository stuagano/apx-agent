"""Tests for apx_agent._fleet (fleet selector + helpers)."""
from __future__ import annotations

import pytest

from apx_agent import _fleet


@pytest.mark.unit
def test_to_label_key_adds_prefix_once():
    assert _fleet.to_label_key("team") == "apx.label.team"
    assert _fleet.to_label_key("apx.label.team") == "apx.label.team"


@pytest.mark.unit
def test_is_reserved_flags_system_namespaces():
    assert _fleet.is_reserved("apx.agent.name") is True
    assert _fleet.is_reserved("apx.apps.role") is True
    assert _fleet.is_reserved("team") is False
    assert _fleet.is_reserved("apx.label.team") is False


@pytest.mark.unit
def test_parse_where_splits_key_value():
    assert _fleet.parse_where(["team=revops", "env=prod"]) == {
        "team": "revops",
        "env": "prod",
    }


@pytest.mark.unit
def test_parse_where_rejects_missing_equals():
    with pytest.raises(ValueError, match="key=value"):
        _fleet.parse_where(["teamrevops"])


from types import SimpleNamespace


def _model(name, *, catalog="cat", schema="sch", **tags):
    """Build a fake registered-model object like the SDK returns."""
    full = f"{catalog}.{schema}.{name}"
    return SimpleNamespace(
        name=name,
        catalog_name=catalog,
        schema_name=schema,
        full_name=full,
        tags=[SimpleNamespace(key=k, value=v) for k, v in tags.items()],
    )


@pytest.mark.unit
def test_resolve_skips_models_without_name_tag():
    models = [_model("untagged"), _model("a", **{_fleet.NAME_TAG: "a"})]
    out = _fleet.resolve_agents(models)
    assert [r.name for r in out] == ["a"]


@pytest.mark.unit
def test_resolve_filters_by_where_either_namespace():
    models = [
        _model("a", **{_fleet.NAME_TAG: "a", "apx.label.team": "revops"}),
        _model("b", **{_fleet.NAME_TAG: "b", "apx.label.team": "data"}),
    ]
    out = _fleet.resolve_agents(models, where={"team": "revops"})
    assert [r.name for r in out] == ["a"]


@pytest.mark.unit
def test_resolve_where_is_anded():
    models = [
        _model("a", **{_fleet.NAME_TAG: "a", "apx.label.team": "revops",
                       "apx.label.env": "prod"}),
        _model("b", **{_fleet.NAME_TAG: "b", "apx.label.team": "revops",
                       "apx.label.env": "dev"}),
    ]
    out = _fleet.resolve_agents(models, where={"team": "revops", "env": "prod"})
    assert [r.name for r in out] == ["a"]


@pytest.mark.unit
def test_resolve_name_glob():
    models = [
        _model("p1", **{_fleet.NAME_TAG: "payroll-east"}),
        _model("p2", **{_fleet.NAME_TAG: "revops-bot"}),
    ]
    out = _fleet.resolve_agents(models, name_glob="payroll-*")
    assert [r.name for r in out] == ["payroll-east"]


@pytest.mark.unit
def test_resolve_explicit_uc_names_bypasses_other_filters():
    models = [
        _model("a", catalog="cat", **{_fleet.NAME_TAG: "a"}),
        _model("b", catalog="other", **{_fleet.NAME_TAG: "b"}),
    ]
    out = _fleet.resolve_agents(
        models, catalog="cat", uc_names=["other.sch.b"],
    )
    assert [r.uc_name for r in out] == ["other.sch.b"]


@pytest.mark.unit
def test_resolved_agent_exposes_labels_and_app():
    m = _model("a", **{_fleet.NAME_TAG: "a", _fleet.MODEL_TAG: "ep",
                       _fleet.APP_NAME_TAG: "app-a", "apx.label.team": "revops"})
    (r,) = _fleet.resolve_agents([m])
    assert r.model == "ep"
    assert r.app_name == "app-a"
    assert r.labels == {"team": "revops"}
