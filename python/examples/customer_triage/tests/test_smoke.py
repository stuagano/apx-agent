"""Smoke tests for the customer_triage example.

Verifies the example imports cleanly, exposes the expected surface, and
declares the right resources. Doesn't make any Databricks calls — pure
import-time + introspection checks.

Run from this directory::

    pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _add_example_to_path() -> None:
    """Make agent.py importable from the example directory."""
    example_dir = Path(__file__).parent.parent
    if str(example_dir) not in sys.path:
        sys.path.insert(0, str(example_dir))
    yield
    # Don't pop — re-imports across tests should reuse the cached module.


def test_agent_imports() -> None:
    import agent as agent_module
    assert hasattr(agent_module, "agent"), "expected a top-level `agent` variable"


def test_top_level_agent_is_router() -> None:
    import agent as agent_module
    from apx_agent import RouterAgent

    assert isinstance(agent_module.agent, RouterAgent)


def test_routes_present() -> None:
    import agent as agent_module

    route_names = {name for name, _, _ in agent_module.agent._routes}
    assert route_names == {
        "billing_specialist",
        "technical_specialist",
        "account_specialist",
        "other",
    }


def test_classify_intent_is_uc_synced() -> None:
    import agent as agent_module
    from apx_agent import get_tool_metadata

    meta = get_tool_metadata(agent_module.classify_intent)
    assert meta is not None
    assert meta.uc_name == "main.agent_tools.classify_intent"
    assert "agent_consumers" in meta.grants


def test_classify_intent_routing() -> None:
    import agent as agent_module
    assert agent_module.classify_intent("I want a refund for my last invoice") == "billing"
    assert agent_module.classify_intent("the dashboard is broken") == "technical"
    assert agent_module.classify_intent("forgot my password") == "account"
    assert agent_module.classify_intent("just saying hi") == "other"


def test_declared_resources_include_genie_and_vector_search() -> None:
    import agent as agent_module
    from apx_agent import collect_resource_specs

    specs = collect_resource_specs(agent_module.agent)
    kinds = {s.kind for s in specs}
    assert "genie_space" in kinds  # account_specialist
    assert "vector_search_index" in kinds  # technical_specialist
