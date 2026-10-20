from __future__ import annotations

from apx_agent import Agent, SequentialAgent, platform_tools
from apx_agent._platform_tools import PlatformToolRef, platform_tool_leaves


def _noop(q: str) -> str:
    """Echo."""
    return q


def test_platform_tools_splats_into_tools_and_stays_out_of_callables() -> None:
    agent = Agent(name="a", tools=[_noop, *platform_tools("genie-pricing")])
    assert agent._tool_fns == [_noop]
    assert agent._platform_tool_refs == [PlatformToolRef(frozenset({"genie-pricing"}))]
    assert len(agent._analyzed) == 1


def test_no_ids_means_all() -> None:
    [ref] = platform_tools()
    assert ref.ids == frozenset()


def test_leaves_found_in_composites_including_shared_ids() -> None:
    a = Agent(name="a", tools=[*platform_tools("g")])
    b = Agent(name="b", tools=[*platform_tools("g")])
    leaves = platform_tool_leaves(SequentialAgent([a, b], name="s"))
    assert [(leaf._name, ref.ids) for leaf, ref in leaves] == [
        ("a", frozenset({"g"})), ("b", frozenset({"g"})),
    ]


# --- materialization through AgentKit -------------------------------------------------
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

_BASE = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n'
_SPACE = "01ef0123456789abcdef0123456789ab"


def _genie(tool_id: str) -> str:
    return (
        f'\n[[tools]]\nid = "{tool_id}"\nauth = "user"\n'
        f'[tools.source]\nkind = "genie_agent"\nspace_id = "{_SPACE}"\n'
    )


def test_genie_tools_resolve_identity_per_call(tmp_path: Path) -> None:
    from apx_agent._platform_tools import static_platform_tools

    (tmp_path / "agent.toml").write_text(_BASE + _genie("pricing"))
    calls: list[str] = []

    def client_for(mode: str) -> SimpleNamespace:
        calls.append(mode)
        return SimpleNamespace()

    tools = static_platform_tools(tmp_path, client_for)
    assert [t.name for t in tools["pricing"]] == ["pricing_ask", "pricing_poll", "pricing_query_result"]
    assert calls == []  # construction does no auth; GenieAgent calls client_for at call time


def test_genie_id_containing_query_groups_under_its_id(tmp_path: Path) -> None:
    from apx_agent._platform_tools import static_platform_tools

    (tmp_path / "agent.toml").write_text(_BASE + _genie("my_query"))
    tools = static_platform_tools(tmp_path, lambda mode: SimpleNamespace())
    assert list(tools) == ["my_query"]
    assert len(tools["my_query"]) == 3


def test_project_root_is_pinned(tmp_path: Path, monkeypatch) -> None:
    import os

    from apx_agent._platform_tools import static_platform_tools

    (tmp_path / "agent.toml").write_text(_BASE + _genie("pricing"))
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    static_platform_tools(tmp_path, lambda mode: SimpleNamespace())
    assert "AGENTBRICKS_PROJECT_ROOT" not in os.environ  # pinned only for the load, then restored


def test_private_mcp_seam_still_exists() -> None:
    """Fails loudly when a databricks-agentbricks bump removes the one private symbol we use."""
    from databricks_agentkit.langgraph import mcp

    assert callable(mcp._server_from_tool)
