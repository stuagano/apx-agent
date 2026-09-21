"""Contract tests for the pe55-support-agent example (PE55 Brickfood)."""

from __future__ import annotations

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

from apx_agent import Agent
from apx_agent._resources import collect_resource_specs, get_user_api_scopes

EXAMPLE_DIR = Path(__file__).parents[1] / "examples/pe55-support-agent"
AGENT_PY = EXAMPLE_DIR / "agent.py"


def _load_example(*, smoke: str | None = None, extra_env: dict[str, str] | None = None):
    assert AGENT_PY.exists(), "pe55-support-agent example is missing"
    env_backup = os.environ.copy()
    try:
        if smoke is None:
            os.environ.pop("APX_SMOKE_MODE", None)
        else:
            os.environ["APX_SMOKE_MODE"] = smoke
        if extra_env:
            os.environ.update(extra_env)
        spec = spec_from_file_location(
            f"pe55_support_agent_example_{uuid4().hex}", AGENT_PY
        )
        assert spec is not None and spec.loader is not None
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(env_backup)


def test_import_without_workspace_builds_a_real_agent() -> None:
    example = _load_example(smoke="0")
    assert example.agent is not None
    assert isinstance(example.agent, Agent)
    assert example.agent._name == "pe55_support_agent"


def test_live_tools_are_policy_sql_and_vector_search() -> None:
    example = _load_example(smoke="0")
    names = [fn.__name__ for fn in example.agent._tool_fns]
    assert names == ["load_latest_policies", "run_sql", "vector_search"]


def test_live_resources_declare_reviews_and_docs() -> None:
    example = _load_example(smoke="0")
    specs = {(spec.kind, spec.identifier) for spec in collect_resource_specs(example.agent)}
    assert ("uc_table", "agent_cuj.customer_support.user_reviews") in specs
    assert ("vector_search_index", "agent_cuj.knowledge.product_docs") in specs


def test_policy_tool_declares_files_scope() -> None:
    example = _load_example(smoke="0")
    policy = next(
        fn for fn in example.agent._tool_fns if fn.__name__ == "load_latest_policies"
    )
    assert "files" in get_user_api_scopes(policy)


def test_smoke_mode_policy_returns_stub_text() -> None:
    example = _load_example(smoke="1")
    assert isinstance(example.agent, Agent)
    policy = next(
        fn for fn in example.agent._tool_fns if fn.__name__ == "load_latest_policies"
    )
    text = policy()
    assert "smoke stub" in text.lower()
    assert "refund" in text.lower()


def test_source_does_not_invent_table_sql_factory_or_region_filter() -> None:
    source = AGENT_PY.read_text()
    assert 'sql_tool("' not in source
    assert "volume_read_tool" not in source
    assert "volume_file_tool" not in source
    assert "WHERE region" not in source
    assert "allowed_regions" in source
    assert "files.download" in source
    assert "APX_SMOKE_MODE" in source
    assert "create_app(None)" not in source
