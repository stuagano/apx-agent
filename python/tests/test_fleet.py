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


@pytest.mark.unit
def test_summary_exit_code_zero_when_all_ok():
    outcomes = [
        _fleet.AgentOutcome("a.b.c", "ok", "tagged"),
        _fleet.AgentOutcome("a.b.d", "skipped", "no change"),
    ]
    rendered = _fleet.render_summary(outcomes, apply=True)
    assert rendered.exit_code == 0
    assert "1 ok" in rendered.text and "1 skipped" in rendered.text


@pytest.mark.unit
def test_summary_exit_code_nonzero_on_failure():
    outcomes = [
        _fleet.AgentOutcome("a.b.c", "ok", "tagged"),
        _fleet.AgentOutcome("a.b.d", "failed", "boom"),
    ]
    rendered = _fleet.render_summary(outcomes, apply=True)
    assert rendered.exit_code == 1
    assert "1 failed" in rendered.text
    assert "boom" in rendered.text


@pytest.mark.unit
def test_summary_marks_dry_run():
    rendered = _fleet.render_summary(
        [_fleet.AgentOutcome("a.b.c", "ok", "would tag")], apply=False,
    )
    assert "dry-run" in rendered.text.lower()


from unittest.mock import MagicMock, patch
from click.testing import CliRunner

from apx_agent.cli import main


def _fake_ws(models):
    ws = MagicMock()
    ws.registered_models.list.return_value = iter(models)
    return ws


@pytest.mark.unit
def test_fleet_list_prints_selected_agents():
    ws = _fake_ws([
        _model("a", **{_fleet.NAME_TAG: "payroll", "apx.label.team": "revops"}),
        _model("b", **{_fleet.NAME_TAG: "other"}),
    ])
    with patch("apx_agent.cli._require_sdk", return_value=ws):
        result = CliRunner().invoke(
            main, ["fleet", "list", "--where", "team=revops"],
        )
    assert result.exit_code == 0, result.output
    assert "payroll" in result.output
    assert "other" not in result.output


@pytest.mark.unit
def test_fleet_list_json_format():
    ws = _fake_ws([_model("a", **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws):
        result = CliRunner().invoke(main, ["fleet", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert '"payroll"' in result.output


@pytest.mark.unit
def test_agents_list_still_discovers_by_name_tag():
    ws = _fake_ws([
        _model("a", **{_fleet.NAME_TAG: "payroll", _fleet.MODEL_TAG: "ep",
                       "apx.agent.tool_count": "3"}),
        _model("b"),  # untagged -> excluded
    ])
    with patch("apx_agent.cli._require_sdk", return_value=ws):
        result = CliRunner().invoke(main, ["agents", "list"])
    assert result.exit_code == 0, result.output
    assert "payroll" in result.output
    assert "ep" in result.output


@pytest.mark.unit
def test_fleet_tag_dry_run_writes_nothing():
    ws = _fake_ws([_model("a", **{_fleet.NAME_TAG: "payroll"})])
    client = MagicMock()
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "tag", "--name", "payroll", "--set", "team=revops"],
        )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    client.set_registered_model_tag.assert_not_called()


@pytest.mark.unit
def test_fleet_tag_apply_sets_label():
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    client = MagicMock()
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "tag", "--name", "payroll",
                   "--set", "team=revops", "--apply"],
        )
    assert result.exit_code == 0, result.output
    client.set_registered_model_tag.assert_called_once_with(
        "cat.sch.a", "apx.label.team", "revops",
    )


@pytest.mark.unit
def test_fleet_tag_json_format():
    import json

    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    client = MagicMock()
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "tag", "--name", "payroll",
                   "--set", "team=revops", "--apply", "--format", "json"],
        )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is True
    assert payload["results"][0]["uc_name"] == "cat.sch.a"
    assert payload["results"][0]["status"] == "ok"
    assert payload["summary"] == {"ok": 1, "skipped": 0, "failed": 0}


@pytest.mark.unit
def test_fleet_tag_refuses_reserved_namespace():
    ws = _fake_ws([_model("a", **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws):
        result = CliRunner().invoke(
            main, ["fleet", "tag", "--name", "payroll",
                   "--set", "apx.agent.name=x", "--apply"],
        )
    assert result.exit_code != 0
    assert "reserved" in result.output.lower()


def _fake_mlflow_model(tags: dict):
    return SimpleNamespace(
        tags=[SimpleNamespace(key=k, value=v) for k, v in tags.items()],
    )


@pytest.mark.unit
def test_fleet_backfill_stamps_missing_identity_tags():
    client = MagicMock()
    client.get_registered_model.return_value = _fake_mlflow_model({})  # no tags
    with patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "backfill", "--uc-name", "cat.sch.payroll",
                   "--name", "payroll", "--app", "payroll-app", "--apply"],
        )
    assert result.exit_code == 0, result.output
    calls = {c.args[1]: c.args[2]
             for c in client.set_registered_model_tag.call_args_list}
    assert calls["apx.agent.name"] == "payroll"
    assert calls["apx.apps.app_name"] == "payroll-app"
    assert calls["apx.serving"] == "apps"


@pytest.mark.unit
def test_fleet_backfill_dry_run_writes_nothing():
    client = MagicMock()
    client.get_registered_model.return_value = _fake_mlflow_model({})
    with patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "backfill", "--uc-name", "cat.sch.payroll",
                   "--name", "payroll"],
        )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    client.set_registered_model_tag.assert_not_called()


@pytest.mark.unit
def test_fleet_backfill_json_format_omits_text_note():
    import json

    client = MagicMock()
    client.get_registered_model.return_value = _fake_mlflow_model({})
    with patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "backfill", "--uc-name", "cat.sch.payroll",
                   "--name", "payroll", "--format", "json"],
        )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is False
    assert payload["results"][0]["uc_name"] == "cat.sch.payroll"
    # The human-only "cannot reconstruct tools/resources" note must not leak
    # into the JSON stream.
    assert "reconstruct" not in result.output


@pytest.mark.unit
def test_fleet_backfill_requires_uc_name():
    result = CliRunner().invoke(main, ["fleet", "backfill", "--name", "x"])
    assert result.exit_code != 0
    assert "uc-name" in result.output.lower()


@pytest.mark.unit
def test_fleet_repoint_promotes_when_latest_differs():
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_prod_version", return_value="5"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="3"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "repoint", "--apply"])
    assert result.exit_code == 0, result.output
    setp.assert_called_once_with("cat.sch.a", "5")
    assert "3" in result.output and "5" in result.output


@pytest.mark.unit
def test_fleet_repoint_skips_when_already_latest():
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_prod_version", return_value="5"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="5"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "repoint", "--apply"])
    assert result.exit_code == 0, result.output
    setp.assert_not_called()
    assert "skipped" in result.output.lower()


@pytest.mark.unit
def test_fleet_repoint_skip_reason_names_model_serving():
    """An agent with no prod-tagged versions (model-serving, or never
    prod-deployed) must be skipped WITH the reason, not silently."""
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_prod_version", return_value=None), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value=None), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "repoint", "--apply"])
    assert result.exit_code == 0, result.output
    setp.assert_not_called()
    assert "no prod-tagged versions" in result.output
    assert "model-serving or never prod-deployed" in result.output


@pytest.mark.unit
def test_fleet_repoint_dry_run_writes_nothing():
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_prod_version", return_value="5"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="3"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "repoint"])
    assert result.exit_code == 0, result.output
    setp.assert_not_called()
    assert "dry-run" in result.output.lower()


@pytest.mark.unit
def test_fleet_repoint_json_format():
    import json

    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_prod_version", return_value="5"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="3"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(
            main, ["fleet", "repoint", "--apply", "--format", "json"],
        )
    assert result.exit_code == 0, result.output
    setp.assert_called_once_with("cat.sch.a", "5")
    payload = json.loads(result.output)
    assert payload["results"][0]["uc_name"] == "cat.sch.a"
    assert payload["results"][0]["detail"] == "3 -> 5"


@pytest.mark.unit
def test_fleet_repoint_fail_fast_stops_at_first_error():
    ws = _fake_ws([
        _model("a", catalog="cat", schema="sch", **{_fleet.NAME_TAG: "a"}),
        _model("b", catalog="cat", schema="sch", **{_fleet.NAME_TAG: "b"}),
    ])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_prod_version",
               side_effect=RuntimeError("boom")), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="1"):
        result = CliRunner().invoke(main, ["fleet", "repoint", "--apply", "--fail-fast"])
    assert result.exit_code == 1
    assert result.output.count("failed") >= 1


@pytest.mark.unit
def test_fleet_repoint_uses_prod_version_not_any_role():
    """Regression guard: @prod must advance to the latest PROD version, never
    a canary. fleet repoint must use get_latest_prod_version, not
    get_latest_apps_version (which maxes over all roles incl. canary)."""
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_apps_version") as any_role, \
         patch("apx_agent._apps_registry.get_latest_prod_version", return_value="4"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="2"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "repoint", "--apply"])
    assert result.exit_code == 0, result.output
    any_role.assert_not_called()
    setp.assert_called_once_with("cat.sch.a", "4")


@pytest.mark.unit
def test_fleet_redeploy_alias_still_works_and_warns():
    """`fleet redeploy` is a hidden deprecated alias: same repoint behavior,
    plus a one-line deprecation warning saying what the command really does."""
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_prod_version", return_value="5"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="3"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "redeploy", "--apply"])
    assert result.exit_code == 0, result.output
    setp.assert_called_once_with("cat.sch.a", "5")
    assert "renamed to `fleet repoint`" in result.output
    assert "does not rebuild or redeploy" in result.output


@pytest.mark.unit
def test_fleet_redeploy_alias_hidden_from_help():
    result = CliRunner().invoke(main, ["fleet", "--help"])
    assert result.exit_code == 0, result.output
    assert "repoint" in result.output
    assert "redeploy" not in result.output
