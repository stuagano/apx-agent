"""Genie Agents as code (#1257): offline validation, pull round-trip, plan/diff,
create vs update, dry-run, and the CLI — all with the Genie SDK mocked."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from ctk import Artifact, verify
from databricks.sdk.errors import NotFound

from apx_agent import _genie_space_as_code as gs
from apx_agent.cli import main

HOST = "https://dev.cloud.databricks.com"
PROD = "https://prod.cloud.databricks.com"


def _id(n: int) -> str:
    return f"{n:032x}"


def _space() -> dict[str, Any]:
    return {
        "version": 2,
        "config": {"sample_questions": [{"id": _id(1), "question": ["Top customers?"]}]},
        "data_sources": {
            "tables": [
                {
                    "identifier": "sales.analytics.customers",
                    "column_configs": [{"column_name": "name"}, {"column_name": "region"}],
                },
                {"identifier": "sales.analytics.orders", "description": ["One row per order"]},
            ],
        },
        "instructions": {
            "text_instructions": [{"id": _id(10), "content": ["Amounts are USD."]}],
            "example_question_sqls": [
                {"id": _id(11), "question": ["Revenue?"], "sql": ["SELECT SUM(amount) FROM sales.analytics.orders"]}
            ],
            "join_specs": [
                {
                    "id": _id(12),
                    "left": {"identifier": "sales.analytics.orders", "alias": "orders"},
                    "right": {"identifier": "sales.analytics.customers", "alias": "customers"},
                    "sql": [
                        "`orders`.`customer_id` = `customers`.`customer_id`",
                        "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
                    ],
                }
            ],
            "sql_snippets": {"measures": [{"id": _id(13), "alias": "revenue", "sql": ["SUM(amount)"]}]},
        },
        "benchmarks": {
            "questions": [
                {"id": _id(2), "question": ["Orders?"], "answer": [{"format": "SQL", "content": ["SELECT 1"]}]}
            ]
        },
    }


def _config(**overrides: Any) -> gs.GenieSpaceConfig:
    values: dict[str, Any] = {
        "title": "Sales", "warehouse_id": "wh-dev", "serialized_space": _space(),
        "description": "Sales analytics",
    }
    values.update(overrides)
    return gs.GenieSpaceConfig(**values)


def _live(space_id: str, space: dict[str, Any], *, title: str = "Sales", warehouse: str = "wh-dev",
          description: str = "Sales analytics") -> SimpleNamespace:
    return SimpleNamespace(space_id=space_id, title=title, description=description,
                           warehouse_id=warehouse, serialized_space=json.dumps(space), etag="e1")


def _ws(spaces: dict[str, SimpleNamespace], host: str = HOST) -> MagicMock:
    ws = MagicMock(name="ws")
    ws.config.host = host + "/"

    def get_space(space_id: str, include_serialized_space: bool = False) -> SimpleNamespace:
        if space_id not in spaces:
            raise NotFound(f"no space {space_id}")
        return spaces[space_id]

    ws.genie.get_space.side_effect = get_space
    ws.genie.list_spaces.return_value = SimpleNamespace(
        spaces=[SimpleNamespace(space_id=s.space_id, title=s.title) for s in spaces.values()],
        next_page_token=None,
    )
    ws.genie.create_space.return_value = SimpleNamespace(space_id="new-space")
    return ws


# --- validation -----------------------------------------------------------


def test_valid_space_passes() -> None:
    assert gs.validate_serialized_space(_space()) == []
    assert gs.validate_config(_config()) == []


def _break(mutate: Any) -> list[str]:
    space = _space()
    mutate(space)
    return gs.validate_serialized_space(space)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s.update(version=1), "version: must be 2"),
        (lambda s: s.pop("version"), "version: must be 2, got None"),
        (lambda s: s["config"]["sample_questions"][0].update(id="A" * 32), "32-character lowercase hex"),
        (lambda s: s["config"]["sample_questions"][0].update(id="a1b2c3d4e5f6"), "32-character lowercase hex"),
        (lambda s: s["config"]["sample_questions"][0].update(id="a1b2c3d4-e5f6-0000-0000-00000000000a"),
         "without hyphens"),
        (lambda s: s["data_sources"]["tables"].reverse(), "data_sources.tables: must be sorted by identifier"),
        (lambda s: s["data_sources"]["tables"][0]["column_configs"].reverse(),
         "column_configs: must be sorted by column_name"),
        (lambda s: s["instructions"]["example_question_sqls"].insert(
            0, {"id": _id(99), "question": ["q"], "sql": ["SELECT 1"]}),
         "instructions.example_question_sqls: must be sorted by id"),
        # sample_questions and benchmarks share an ID space
        (lambda s: s["benchmarks"]["questions"][0].update(id=_id(1)), f"{_id(1)} duplicates config.sample_questions[0]"),
        # instruction collections share another
        (lambda s: s["instructions"]["join_specs"][0].update(id=_id(11)),
         "duplicates instructions.example_question_sqls[0]"),
        (lambda s: s["data_sources"]["tables"][0]["column_configs"].append({"column_name": "region"}),
         "column sales.analytics.customers.region is already configured"),
        (lambda s: s["instructions"]["text_instructions"].append({"id": _id(20), "content": ["more"]}),
         "at most 1 text instruction"),
        (lambda s: s["instructions"]["text_instructions"][0].update(content=["x" * 25_001]),
         "string is 25001 characters; the limit is 25000"),
        (lambda s: s["data_sources"]["tables"][1].update(description=["d"] * 10_001),
         "10001 items; the limit is 10000"),
        (lambda s: s["data_sources"]["tables"][1].update(identifier="analytics.orders"),
         "three-level catalog.schema.table"),
        (lambda s: s["instructions"]["join_specs"][0]["sql"].pop(), "must be exactly two strings"),
        (lambda s: s["instructions"]["join_specs"][0]["sql"].__setitem__(1, "MANY_TO_ONE"),
         "must be a relationship annotation"),
        (lambda s: s["instructions"]["join_specs"][0]["sql"].__setitem__(0, "orders.customer_id = customers.id"),
         "must reference alias 'orders' backtick-quoted"),
        (lambda s: s["instructions"]["join_specs"][0]["right"].pop("alias"), "right.alias: required"),
        (lambda s: s["instructions"]["sql_snippets"]["measures"][0].update(sql=[" "]),
         "measures[0].sql: must be a non-empty list"),
        (lambda s: s["benchmarks"]["questions"][0]["answer"][0].update(format="TEXT"),
         "exactly one answer with format 'SQL'"),
        (lambda s: s["data_sources"].update(mcp_connections=[{"guidance": "g"}]),
         "mcp_connections[0].connection_name: required"),
    ],
)
def test_rule_violations_are_reported(mutate: Any, expected: str) -> None:
    errors = _break(mutate)
    assert any(expected in e for e in errors), errors


def test_config_requires_title_and_warehouse() -> None:
    errors = gs.validate_config(_config(title=" ", warehouse_id=""))
    assert "title: required" in errors and "warehouse_id: required" in errors


def test_load_rejects_unknown_and_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "space.yml"
    path.write_text("titel: Sales\nserialized_space: {version: 2}\n")
    with pytest.raises(gs.GenieSpaceConfigError) as info:
        gs.load_config(path)
    message = str(info.value)
    assert "titel: unknown key" in message and "title: required string" in message
    assert "warehouse_id: required string" in message


def test_docs_example_config_is_valid(tmp_path: Path) -> None:
    doc = Path(__file__).parents[2] / "docs" / "deploy" / "genie-agents-as-code.md"
    example = doc.read_text().split("```yaml\n", 1)[1].split("```", 1)[0]
    path = tmp_path / "example.yml"
    path.write_text(example)
    config = gs.load_config(path)
    assert gs.validate_config(config) == []
    assert config.deployments == {"https://dev.cloud.databricks.com": "01f0c0b4e8151000000000000000000a"}


# --- pull round trip -----------------------------------------------------


@pytest.mark.parametrize("suffix", [".yml", ".json"])
def test_pull_round_trips_through_file(tmp_path: Path, suffix: str) -> None:
    ws = _ws({"s1": _live("s1", _space())})
    path = tmp_path / f"sales{suffix}"
    gs.dump_config(gs.pull(ws, "s1"), path)

    verify(Artifact(str(path), min_bytes=200, must_contain="FROM_RELATIONSHIP_TYPE_MANY_TO_ONE"))
    text = path.read_text()
    assert "\\\"" not in text  # serialized_space is stored unescaped, not as a JSON string
    loaded = gs.load_config(path)
    assert loaded.serialized_space == _space()
    assert loaded.title == "Sales" and loaded.warehouse_id == "wh-dev"
    assert loaded.deployments == {HOST: "s1"}
    assert gs.plan(ws, loaded).action == "noop"


def test_pull_keeps_other_workspace_deployments() -> None:
    ws = _ws({"s1": _live("s1", _space())})
    pulled = gs.pull(ws, "s1", _config(deployments={PROD: "p9"}))
    assert pulled.deployments == {PROD: "p9", HOST: "s1"}


def test_pull_without_serialized_space_explains() -> None:
    live = _live("s1", _space())
    live.serialized_space = None
    with pytest.raises(gs.GenieSpaceConfigError, match="edit permission"):
        gs.pull(_ws({"s1": live}), "s1")


# --- plan / push ---------------------------------------------------------


def test_plan_create_when_nothing_matches() -> None:
    result = gs.plan(_ws({}), _config())
    assert result.action == "create" and result.space_id is None
    assert '+  "title": "Sales"' in result.diff


def test_plan_update_by_recorded_id_shows_diff() -> None:
    changed = _space()
    changed["instructions"]["text_instructions"][0]["content"] = ["Amounts are EUR."]
    ws = _ws({"s1": _live("s1", _space(), title="Old title")})
    result = gs.plan(ws, _config(serialized_space=changed, deployments={HOST: "s1"}))
    assert (result.action, result.space_id, result.matched_by) == ("update", "s1", "recorded id")
    lines = result.diff.splitlines()
    assert any(line.startswith("-") and '"Amounts are USD."' in line for line in lines)
    assert any(line.startswith("+") and '"Amounts are EUR."' in line for line in lines)
    assert '-  "title": "Old title",' in lines and '+  "title": "Sales",' in lines
    ws.genie.list_spaces.assert_not_called()


def test_plan_matches_by_title_and_falls_back_from_stale_id() -> None:
    ws = _ws({"s1": _live("s1", _space())})
    result = gs.plan(ws, _config(deployments={HOST: "gone"}))
    assert (result.action, result.space_id, result.matched_by) == ("noop", "s1", "title")


def test_plan_ambiguous_title_refuses() -> None:
    ws = _ws({"a": _live("a", _space()), "b": _live("b", _space())})
    with pytest.raises(gs.GenieSpaceConfigError, match="2 Genie spaces are titled 'Sales'"):
        gs.plan(ws, _config())


def test_title_match_paginates() -> None:
    ws = _ws({"s1": _live("s1", _space())})
    ws.genie.list_spaces.side_effect = [
        SimpleNamespace(spaces=[SimpleNamespace(space_id="x", title="Other")], next_page_token="p2"),
        SimpleNamespace(spaces=[SimpleNamespace(space_id="s1", title="Sales")], next_page_token=None),
    ]
    assert gs.plan(ws, _config()).space_id == "s1"
    assert ws.genie.list_spaces.call_args_list[1].kwargs == {"page_token": "p2"}


def test_push_creates_with_escaped_serialized_space() -> None:
    ws = _ws({})
    result = gs.push(ws, _config(parent_path="/Workspace/Shared"))
    assert (result.action, result.space_id) == ("create", "new-space")
    kwargs = ws.genie.create_space.call_args.kwargs
    assert json.loads(kwargs["serialized_space"]) == _space()
    assert kwargs["warehouse_id"] == "wh-dev" and kwargs["parent_path"] == "/Workspace/Shared"
    ws.genie.update_space.assert_not_called()
    ws.genie.trash_space.assert_not_called()


def test_push_updates_existing_space() -> None:
    changed = copy.deepcopy(_space())
    changed["config"]["sample_questions"][0]["question"] = ["Biggest customers?"]
    ws = _ws({"s1": _live("s1", _space())})
    result = gs.push(ws, _config(serialized_space=changed, deployments={HOST: "s1"}))
    assert (result.action, result.space_id) == ("update", "s1")
    args, kwargs = ws.genie.update_space.call_args
    assert args == ("s1",) and json.loads(kwargs["serialized_space"]) == changed
    ws.genie.create_space.assert_not_called()
    ws.genie.trash_space.assert_not_called()


def test_push_noop_and_dry_run_make_no_writes() -> None:
    ws = _ws({"s1": _live("s1", _space())})
    assert gs.push(ws, _config()).action == "noop"
    assert gs.push(_ws({}), _config(), dry_run=True).action == "create"
    dry = _ws({"s1": _live("s1", _space())})
    assert gs.push(dry, _config(title="Sales", description="new"), dry_run=True).action == "update"
    for client in (ws, dry):
        client.genie.create_space.assert_not_called()
        client.genie.update_space.assert_not_called()


def test_push_refuses_invalid_config_before_calling_workspace() -> None:
    ws = _ws({})
    broken = _space()
    broken["version"] = 1
    with pytest.raises(gs.GenieSpaceConfigError, match="serialized_space.version: must be 2"):
        gs.push(ws, _config(serialized_space=broken))
    ws.genie.list_spaces.assert_not_called()


def test_push_warehouse_override_for_promotion() -> None:
    ws = _ws({}, host=PROD)
    gs.push(ws, _config(deployments={HOST: "s1"}), warehouse_id="wh-prod")
    assert ws.genie.create_space.call_args.kwargs["warehouse_id"] == "wh-prod"
    ws.genie.get_space.assert_not_called()  # the dev ID is not looked up on prod


# --- CLI ------------------------------------------------------------------


def test_cli_validate(tmp_path: Path) -> None:
    good = tmp_path / "good.yml"
    gs.dump_config(_config(), good)
    result = CliRunner().invoke(main, ["resources", "genie-space", "validate", str(good)])
    assert result.exit_code == 0 and "valid" in result.output

    space = _space()
    space["data_sources"]["tables"].reverse()
    bad = tmp_path / "bad.yml"
    gs.dump_config(_config(serialized_space=space), bad)
    result = CliRunner().invoke(main, ["resources", "genie-space", "validate", str(bad)])
    assert result.exit_code == 1
    assert "serialized_space.data_sources.tables: must be sorted by identifier" in result.output


def test_cli_pull_plan_push_records_ids_per_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = _ws({"s1": _live("s1", _space())})
    prod = _ws({}, host=PROD)
    clients = {"dev": dev, "prod": prod}
    monkeypatch.setattr(gs, "_client", lambda profile: clients[profile])
    path = tmp_path / "sales.yml"
    runner = CliRunner()

    result = runner.invoke(main, ["resources", "genie-space", "pull", "s1", str(path), "--profile", "dev"])
    assert result.exit_code == 0, result.output
    path.write_text("# owned by the sales team\n" + path.read_text())

    result = runner.invoke(main, ["resources", "genie-space", "plan", str(path), "--profile", "dev"])
    assert result.exit_code == 0 and result.output.startswith("noop s1 (matched by recorded id)")

    args = ["resources", "genie-space", "push", str(path), "--profile", "prod", "--warehouse", "wh-prod"]
    result = runner.invoke(main, [*args, "--dry-run"])
    assert result.exit_code == 0 and "dry run: no changes made" in result.output
    prod.genie.create_space.assert_not_called()
    assert PROD not in path.read_text()

    result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    assert prod.genie.create_space.call_args.kwargs["warehouse_id"] == "wh-prod"
    verify(Artifact(str(path), min_bytes=200, must_contain="# owned by the sales team"))
    loaded = gs.load_config(path)
    assert loaded.deployments == {HOST: "s1", PROD: "new-space"}
    assert loaded.warehouse_id == "wh-dev"  # the override is not written back
