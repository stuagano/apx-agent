"""Tests for _resources.py — auto-derived MLflow resource declarations.

Covers:

  1. Platform tool factories attach ``_apx_resources`` correctly:
       * genie_tool       → ResourceSpec("genie_space", <space_id>)
       * uc_function_tool → ResourceSpec("uc_function", <function_name>)
  2. ResourceSpec rejects unknown kinds and empty identifiers.
  3. attach_resources / get_resources round-trip on a plain function.
  4. collect_resource_specs walks the agent tree:
       * LlmAgent direct tools
       * SequentialAgent / ParallelAgent / LoopAgent / RouterAgent / HandoffAgent
       * The model serving endpoint is included when ``model=...`` is given
       * Sub-agent URLs become serving_endpoint specs
       * Apps URLs are skipped (no MLflow resource for them)
       * Duplicates are deduped, order preserved
  5. mlflow_resources_for materialises specs into the correct
     ``mlflow.models.resources.Databricks*`` classes (skipped without mlflow).
"""

from __future__ import annotations

from typing import Any

import pytest

from apx_agent import (
    Agent,
    LoopAgent,
    ParallelAgent,
    ResourceSpec,
    SequentialAgent,
    attach_resources,
    catalog_tool,
    collect_resource_specs,
    genie_tool,
    lineage_tool,
    schema_tool,
    uc_function_tool,
)
from apx_agent._resources import (
    _sub_agent_to_endpoint,
    get_resources,
)


# ---------------------------------------------------------------------------
# ResourceSpec validation
# ---------------------------------------------------------------------------


def test_resource_spec_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="Unknown resource kind"):
        ResourceSpec(kind="not_a_real_kind", identifier="x")


def test_resource_spec_rejects_empty_identifier() -> None:
    with pytest.raises(ValueError, match="non-empty identifier"):
        ResourceSpec(kind="uc_function", identifier="")


def test_resource_spec_is_hashable_for_dedup() -> None:
    a = ResourceSpec("uc_function", "main.tools.f")
    b = ResourceSpec("uc_function", "main.tools.f")
    assert a == b
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# Tool factory annotations
# ---------------------------------------------------------------------------


def test_genie_tool_declares_genie_space_resource() -> None:
    tool = genie_tool("space-abc-123")
    specs = get_resources(tool)
    assert specs == [ResourceSpec("genie_space", "space-abc-123")]


def test_uc_function_tool_declares_uc_function_resource() -> None:
    tool = uc_function_tool("main.tools.classify_intent")
    specs = get_resources(tool)
    assert specs == [ResourceSpec("uc_function", "main.tools.classify_intent")]


def test_metadata_tools_declare_no_resources() -> None:
    # lineage/schema/catalog use UC REST APIs gated by user grants only —
    # nothing to declare as a logged resource.
    assert get_resources(lineage_tool()) == []
    assert get_resources(schema_tool()) == []
    assert get_resources(catalog_tool("main", "sales")) == []


def test_attach_resources_round_trip_on_plain_fn() -> None:
    def my_tool(question: str) -> str:
        return question

    attach_resources(my_tool, [ResourceSpec("uc_table", "main.sales.orders")])
    assert get_resources(my_tool) == [ResourceSpec("uc_table", "main.sales.orders")]


def test_attach_resources_appends_rather_than_replaces() -> None:
    def my_tool(question: str) -> str:
        return question

    attach_resources(my_tool, [ResourceSpec("uc_table", "main.sales.orders")])
    attach_resources(my_tool, [ResourceSpec("uc_function", "main.tools.f")])
    assert get_resources(my_tool) == [
        ResourceSpec("uc_table", "main.sales.orders"),
        ResourceSpec("uc_function", "main.tools.f"),
    ]


# ---------------------------------------------------------------------------
# sub_agent URL → endpoint translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_name",
    [
        ("endpoints/data-triage", "data-triage"),
        ("serving-endpoints/data-triage", "data-triage"),
        ("data-triage", "data-triage"),
        ("https://workspace.cloud.databricks.com/serving-endpoints/data-triage/invocations", "data-triage"),
        ("https://workspace.cloud.databricks.com/serving-endpoints/data-triage", "data-triage"),
    ],
)
def test_sub_agent_to_endpoint_translates_serving_forms(raw: str, expected_name: str) -> None:
    spec = _sub_agent_to_endpoint(raw)
    assert spec is not None
    assert spec.kind == "serving_endpoint"
    assert spec.identifier == expected_name


@pytest.mark.parametrize(
    "raw",
    [
        "https://data-triage-1234567890.cloud.databricksapps.com",
        "https://data-triage.workspace.databricksapps.com",
    ],
)
def test_sub_agent_to_endpoint_skips_apps_urls(raw: str) -> None:
    # Apps URLs are not MLflow resources — declared via app-to-app CAN_USE
    # permissions instead, so they should return None here.
    assert _sub_agent_to_endpoint(raw) is None


def test_sub_agent_to_endpoint_expands_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_AGENT_URL", "endpoints/some-agent")
    spec = _sub_agent_to_endpoint("$SOME_AGENT_URL")
    assert spec == ResourceSpec("serving_endpoint", "some-agent")


def test_sub_agent_to_endpoint_returns_none_for_unset_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFINITELY_UNSET_VAR_XYZ", raising=False)
    assert _sub_agent_to_endpoint("$DEFINITELY_UNSET_VAR_XYZ") is None


# ---------------------------------------------------------------------------
# Agent tree walking
# ---------------------------------------------------------------------------


def _plain_tool(question: str) -> str:
    """A plain tool with no resources."""
    return question


def test_collect_includes_model_endpoint() -> None:
    agent = Agent(tools=[_plain_tool])
    specs = collect_resource_specs(agent, model="databricks-claude-sonnet-4-6")
    assert ResourceSpec("serving_endpoint", "databricks-claude-sonnet-4-6") in specs


def test_collect_includes_tool_resources_from_llm_agent() -> None:
    agent = Agent(
        tools=[
            genie_tool("space-abc"),
            uc_function_tool("main.tools.classify"),
            _plain_tool,
        ]
    )
    specs = collect_resource_specs(agent, model="m")
    assert ResourceSpec("genie_space", "space-abc") in specs
    assert ResourceSpec("uc_function", "main.tools.classify") in specs
    assert ResourceSpec("serving_endpoint", "m") in specs


def test_collect_walks_sequential_agent() -> None:
    a = Agent(tools=[genie_tool("space-1")])
    b = Agent(tools=[uc_function_tool("main.tools.x")])
    seq = SequentialAgent([a, b])
    specs = collect_resource_specs(seq, model="m")
    assert ResourceSpec("genie_space", "space-1") in specs
    assert ResourceSpec("uc_function", "main.tools.x") in specs


def test_collect_walks_parallel_agent() -> None:
    a = Agent(tools=[genie_tool("space-1")])
    b = Agent(tools=[uc_function_tool("main.tools.x")])
    par = ParallelAgent([a, b])
    specs = collect_resource_specs(par, model="m")
    assert ResourceSpec("genie_space", "space-1") in specs
    assert ResourceSpec("uc_function", "main.tools.x") in specs


def test_collect_walks_loop_agent() -> None:
    inner = Agent(tools=[genie_tool("space-loop")])
    loop = LoopAgent(inner, max_iterations=3)
    specs = collect_resource_specs(loop, model="m")
    assert ResourceSpec("genie_space", "space-loop") in specs


def test_collect_dedupes_repeated_resources() -> None:
    # Same UC function registered twice — only one resource entry.
    agent = Agent(
        tools=[
            uc_function_tool("main.tools.same", name="first"),
            uc_function_tool("main.tools.same", name="second"),
        ]
    )
    specs = collect_resource_specs(agent, model="m")
    uc_specs = [s for s in specs if s.kind == "uc_function"]
    assert uc_specs == [ResourceSpec("uc_function", "main.tools.same")]


def test_collect_preserves_first_occurrence_order() -> None:
    agent = Agent(
        tools=[
            genie_tool("space-1"),
            uc_function_tool("main.tools.a"),
        ]
    )
    specs = collect_resource_specs(agent, model="m")
    kinds = [s.kind for s in specs]
    # model endpoint is added first, then tool resources in registration order
    assert kinds == ["serving_endpoint", "genie_space", "uc_function"]


def test_collect_includes_sub_agent_endpoints() -> None:
    agent = Agent(
        tools=[_plain_tool],
        sub_agents=["endpoints/data-triage", "serving-endpoints/billing"],
    )
    specs = collect_resource_specs(agent, model="m")
    assert ResourceSpec("serving_endpoint", "data-triage") in specs
    assert ResourceSpec("serving_endpoint", "billing") in specs


def test_collect_skips_apps_sub_agents() -> None:
    agent = Agent(
        tools=[_plain_tool],
        sub_agents=["https://data-triage-12345678.cloud.databricksapps.com"],
    )
    specs = collect_resource_specs(agent, model="m")
    # No serving_endpoint entry except the model itself
    endpoint_idents = [s.identifier for s in specs if s.kind == "serving_endpoint"]
    assert endpoint_idents == ["m"]


def test_collect_accepts_extra_resources() -> None:
    agent = Agent(tools=[_plain_tool])
    extra = [ResourceSpec("sql_warehouse", "wh-12345")]
    specs = collect_resource_specs(agent, model="m", extra=extra)
    assert ResourceSpec("sql_warehouse", "wh-12345") in specs


def test_collect_with_no_model_omits_endpoint() -> None:
    agent = Agent(tools=[uc_function_tool("main.tools.a")])
    specs = collect_resource_specs(agent)
    endpoints = [s for s in specs if s.kind == "serving_endpoint"]
    assert endpoints == []


# ---------------------------------------------------------------------------
# MLflow materialisation — skipped without mlflow installed
# ---------------------------------------------------------------------------


def test_mlflow_resources_for_materialises_each_kind() -> None:
    pytest.importorskip("mlflow")
    from mlflow.models.resources import (
        DatabricksFunction,
        DatabricksGenieSpace,
        DatabricksServingEndpoint,
    )

    from apx_agent import mlflow_resources_for

    agent = Agent(
        tools=[
            genie_tool("space-abc"),
            uc_function_tool("main.tools.classify"),
        ],
        sub_agents=["endpoints/billing"],
    )
    resources = mlflow_resources_for(agent, model="databricks-claude-sonnet-4-6")

    # The materialised list mixes Databricks* types; check by walking.
    found_kinds: set[str] = set()
    for r in resources:
        if isinstance(r, DatabricksFunction):
            found_kinds.add("uc_function")
        elif isinstance(r, DatabricksGenieSpace):
            found_kinds.add("genie_space")
        elif isinstance(r, DatabricksServingEndpoint):
            found_kinds.add("serving_endpoint")

    assert found_kinds == {"uc_function", "genie_space", "serving_endpoint"}


def test_mlflow_resources_for_passes_extra_specs() -> None:
    pytest.importorskip("mlflow")
    from mlflow.models.resources import DatabricksSQLWarehouse

    from apx_agent import mlflow_resources_for

    agent = Agent(tools=[_plain_tool])
    resources = mlflow_resources_for(
        agent,
        model="m",
        extra=[ResourceSpec("sql_warehouse", "wh-prod")],
    )
    warehouses = [r for r in resources if isinstance(r, DatabricksSQLWarehouse)]
    assert len(warehouses) == 1


# ---------------------------------------------------------------------------
# Databricks Apps bundle YAML projection — resources_to_databricks_yml
# ---------------------------------------------------------------------------


from apx_agent import resources_to_databricks_yml  # noqa: E402


def _block_key(entry: dict) -> str:
    """Top-level (and only) key of an entry — e.g. ``serving_endpoint``."""
    keys = [k for k in entry.keys()]
    assert len(keys) == 1, f"expected single typed block, got {keys}"
    return keys[0]


def _block(entry: dict) -> dict:
    return entry[_block_key(entry)]


def test_yml_serving_endpoint_shape() -> None:
    specs = [ResourceSpec("serving_endpoint", "databricks-claude-sonnet-4-6")]
    [entry] = resources_to_databricks_yml(specs)
    assert _block_key(entry) == "serving_endpoint"
    body = _block(entry)
    assert body["endpoint_name"] == "databricks-claude-sonnet-4-6"
    assert body["permission"] == "CAN_QUERY"
    assert body["name"]  # auto-derived slug


def test_yml_uc_function_shape() -> None:
    specs = [ResourceSpec("uc_function", "main.tools.classify_intent")]
    [entry] = resources_to_databricks_yml(specs)
    assert _block_key(entry) == "uc_securable"
    body = _block(entry)
    assert body["securable_full_name"] == "main.tools.classify_intent"
    assert body["securable_type"] == "FUNCTION"
    assert body["permission"] == "EXECUTE"


def test_yml_genie_space_shape() -> None:
    specs = [ResourceSpec("genie_space", "space-abc-123")]
    [entry] = resources_to_databricks_yml(specs)
    assert _block_key(entry) == "genie_space"
    body = _block(entry)
    assert body["space_id"] == "space-abc-123"
    assert body["permission"] == "CAN_RUN"


def test_yml_vector_search_index_shape() -> None:
    specs = [ResourceSpec("vector_search_index", "main.vs.products_index")]
    [entry] = resources_to_databricks_yml(specs)
    assert _block_key(entry) == "uc_securable"
    body = _block(entry)
    assert body["securable_full_name"] == "main.vs.products_index"
    assert body["securable_type"] == "TABLE"
    assert body["permission"] == "SELECT"


def test_yml_sql_warehouse_shape() -> None:
    specs = [ResourceSpec("sql_warehouse", "wh-prod-123")]
    [entry] = resources_to_databricks_yml(specs)
    assert _block_key(entry) == "sql_warehouse"
    body = _block(entry)
    assert body["id"] == "wh-prod-123"
    assert body["permission"] == "CAN_USE"


def test_yml_uc_connection_shape() -> None:
    specs = [ResourceSpec("uc_connection", "main.connections.snowflake_prod")]
    [entry] = resources_to_databricks_yml(specs)
    assert _block_key(entry) == "uc_securable"
    body = _block(entry)
    assert body["securable_full_name"] == "main.connections.snowflake_prod"
    assert body["securable_type"] == "CONNECTION"
    assert body["permission"] == "USE_CONNECTION"


def test_yml_lakebase_instance_shape() -> None:
    specs = [ResourceSpec("lakebase_instance", "prod-lakebase")]
    [entry] = resources_to_databricks_yml(specs)
    assert _block_key(entry) == "database"
    body = _block(entry)
    assert body["instance_name"] == "prod-lakebase"
    assert body["database_name"] == "databricks_postgres"
    assert body["permission"] == "CAN_CONNECT_AND_CREATE"


def test_yml_each_entry_has_name() -> None:
    specs = [
        ResourceSpec("serving_endpoint", "claude-3-5"),
        ResourceSpec("uc_function", "main.tools.foo"),
        ResourceSpec("genie_space", "space-xyz"),
    ]
    yml = resources_to_databricks_yml(specs)
    for entry in yml:
        body = _block(entry)
        assert isinstance(body.get("name"), str)
        assert body["name"]  # non-empty


def test_yml_full_agent_round_trip() -> None:
    """End-to-end: collect_resource_specs → resources_to_databricks_yml."""
    agent = Agent(
        tools=[
            genie_tool("space-abc"),
            uc_function_tool("main.tools.classify"),
        ],
        sub_agents=["endpoints/billing"],
    )
    specs = collect_resource_specs(agent, model="claude-3-5")
    yml = resources_to_databricks_yml(specs)
    # Find the entries by block key
    block_keys = [_block_key(e) for e in yml]
    assert "serving_endpoint" in block_keys
    assert "uc_securable" in block_keys
    assert "genie_space" in block_keys
