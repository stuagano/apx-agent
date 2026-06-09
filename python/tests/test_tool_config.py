import os
import textwrap

import pytest
from apx_agent import Agent
from apx_agent._tool_config import (
    ToolConfigError,
    load_config_tools,
    merge_config_tools,
)


def test_dispatches_single_tool_by_keyword():
    tools = load_config_tools([{"type": "genie", "space_id": "01ef", "name": "ask_sales"}])
    assert len(tools) == 1
    assert tools[0].__name__ == "ask_sales"


def test_flattens_toolkit_lists():
    # sql + a jobs toolkit (returns 4) → 1 + 4 = 5 callables, all flat
    tools = load_config_tools([
        {"type": "sql", "warehouse_id": "wh1"},
        {"type": "jobs", "warehouse_id": "wh1"},
    ])
    assert all(callable(t) for t in tools)
    assert len(tools) == 5


def test_unknown_type_raises_listing_known():
    with pytest.raises(ToolConfigError, match="unknown type 'nope'"):
        load_config_tools([{"type": "nope"}])


def test_missing_type_raises():
    with pytest.raises(ToolConfigError, match="missing 'type'"):
        load_config_tools([{"space_id": "x"}])


def test_missing_required_arg_wrapped_as_config_error():
    with pytest.raises(ToolConfigError, match=r"type=genie.*space_id"):
        load_config_tools([{"type": "genie"}])  # genie_tool needs space_id


def test_empty_input_returns_empty_list():
    assert load_config_tools([]) == []


def test_same_name_collision_requires_explicit_name():
    with pytest.raises(ToolConfigError, match="duplicate tool name"):
        load_config_tools([
            {"type": "vector_search", "index_name": "a.b.c"},
            {"type": "vector_search", "index_name": "a.b.d"},  # both default to "vector_search"
        ])


def test_env_var_resolved_on_string_values(monkeypatch):
    monkeypatch.setenv("SALES_SPACE", "01ef-from-env")
    tools = load_config_tools([{"type": "genie", "space_id": "$SALES_SPACE", "name": "ask"}])
    # The genie tool closes over the resolved space_id; assert via the factory
    # being called with the resolved value by checking no error + a built tool.
    assert tools[0].__name__ == "ask"


def test_allowlist_blocks_disallowed_host(monkeypatch):
    monkeypatch.setenv("APX_TOOLS_ALLOWED_HOSTS", "trusted.example.com")
    with pytest.raises(ToolConfigError, match="not in APX_TOOLS_ALLOWED_HOSTS"):
        load_config_tools([{
            "type": "mcp_toolkit",
            "server_url": "https://evil.example.com/mcp",
        }])


def test_allowlist_unset_allows_any_host(monkeypatch):
    monkeypatch.delenv("APX_TOOLS_ALLOWED_HOSTS", raising=False)
    # No allow-list configured → trusted default → host not checked.
    # Monkeypatch mcp_toolkit so we don't hit the network at factory time.
    import apx_agent._tool_config as mod
    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": lambda **kw: [lambda: None]})
    tools = load_config_tools([{"type": "mcp_toolkit", "server_url": "https://anything/mcp"}])
    assert len(tools) == 1


def test_io_factory_failure_skipped_with_warning(monkeypatch, caplog):
    import logging
    import apx_agent._tool_config as mod

    def boom(**kw):
        raise ConnectionError("server down")

    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": boom})
    monkeypatch.delenv("APX_TOOLS_STRICT", raising=False)
    with caplog.at_level(logging.WARNING, logger="apx_agent._tool_config"):
        tools = load_config_tools([{"type": "mcp_toolkit", "server_url": "https://x/mcp"}])
    assert tools == []
    assert "Skipping tool #0" in caplog.text


def test_io_factory_failure_raises_in_strict_mode(monkeypatch):
    import apx_agent._tool_config as mod

    def boom(**kw):
        raise ConnectionError("server down")

    monkeypatch.setattr(mod, "_registry", lambda: {"mcp_toolkit": boom})
    monkeypatch.setenv("APX_TOOLS_STRICT", "1")
    with pytest.raises(ToolConfigError, match="server down"):
        load_config_tools([{"type": "mcp_toolkit", "server_url": "https://x/mcp"}])


def _write_pyproject(tmp_path, body: str):
    p = tmp_path / "pyproject.toml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_merge_appends_config_tools_to_agent(tmp_path):
    pp = _write_pyproject(tmp_path, """
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """)
    agent = Agent(tools=[])
    merge_config_tools(agent, pyproject_path=pp)
    assert "ask_sales" in [t.name for t in agent.collect_tools()]


def test_merge_is_idempotent(tmp_path):
    pp = _write_pyproject(tmp_path, """
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """)
    agent = Agent(tools=[])
    merge_config_tools(agent, pyproject_path=pp)
    merge_config_tools(agent, pyproject_path=pp)  # second call
    assert [t.name for t in agent.collect_tools()].count("ask_sales") == 1


def test_code_wired_tool_wins_on_name_collision(tmp_path, caplog):
    pp = _write_pyproject(tmp_path, """
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """)
    def ask_sales(q: str) -> str:
        """Code-wired."""
        return q
    agent = Agent(tools=[ask_sales])
    import logging
    with caplog.at_level(logging.WARNING, logger="apx_agent._tool_config"):
        merge_config_tools(agent, pyproject_path=pp)
    assert "already wires a tool with that name" in caplog.text
    # only one ask_sales, and it's the code-wired callable
    assert agent._tool_fns.count(ask_sales) == 1
    assert [t.name for t in agent.collect_tools()].count("ask_sales") == 1


def test_no_tools_section_is_noop(tmp_path):
    pp = _write_pyproject(tmp_path, '[tool.apx.agent]\nname = "t"\n')
    agent = Agent(tools=[])
    merge_config_tools(agent, pyproject_path=pp)  # no [[tool.apx.tools]]
    assert agent.collect_tools() == []


def test_composition_root_warns_and_skips(tmp_path, caplog):
    import logging
    pp = _write_pyproject(tmp_path, """
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """)
    class FakeRoot:
        pass  # no _register_tool, no _tool_fns
    agent = FakeRoot()
    with caplog.at_level(logging.WARNING, logger="apx_agent._tool_config"):
        merge_config_tools(agent, pyproject_path=pp)
    assert "composition root" in caplog.text
    assert not hasattr(agent, "_tool_fns")


def test_public_exports():
    import apx_agent
    assert hasattr(apx_agent, "ToolConfigError")
    assert hasattr(apx_agent, "finalize_agent")
    assert hasattr(apx_agent, "merge_config_tools")
    assert hasattr(apx_agent, "load_config_tools")


# ---------------------------------------------------------------------------
# Change 1: cwd walk-up discovery
# ---------------------------------------------------------------------------


def test_walk_up_finds_pyproject_from_subdir(tmp_path, monkeypatch):
    """merge_config_tools(pyproject_path=None) must walk up from cwd so a
    subdir of the project root can still find [[tool.apx.tools]]."""
    _write_pyproject(tmp_path, """
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales"
    """)
    subdir = tmp_path / "src" / "mypackage"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    agent = Agent(tools=[])
    merge_config_tools(agent, pyproject_path=None)
    assert "ask_sales" in [t.name for t in agent.collect_tools()]


# ---------------------------------------------------------------------------
# Change 2: idempotency sentinel skips I/O factories on repeat calls
# ---------------------------------------------------------------------------


def test_factory_called_only_once_on_repeat_merge(tmp_path, monkeypatch):
    """merge_config_tools must set a sentinel after the first merge so a
    second call returns immediately — never invoking load_config_tools again."""
    import apx_agent._tool_config as mod

    call_count = [0]

    def counting_factory(**kw):
        call_count[0] += 1

        def ask_sales_sentinel(q: str) -> str:
            """Counted factory tool."""
            return q

        return ask_sales_sentinel

    monkeypatch.setattr(mod, "_registry", lambda: {"genie": counting_factory})

    pp = _write_pyproject(tmp_path, """
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef"
        name = "ask_sales_sentinel"
    """)

    agent = Agent(tools=[])
    merge_config_tools(agent, pyproject_path=pp)
    merge_config_tools(agent, pyproject_path=pp)  # second call — must be a no-op

    assert call_count[0] == 1, (
        f"Factory was called {call_count[0]} times; expected 1 — "
        "second merge_config_tools call did not skip via sentinel."
    )


# ---------------------------------------------------------------------------
# skill_tool factory
# ---------------------------------------------------------------------------


def test_skill_tool_returns_markdown_content(tmp_path):
    """skill_tool callable reads and returns the file contents at call time."""
    from apx_agent._tool_config import skill_tool

    md = tmp_path / "guide.md"
    md.write_text("# SQL Guide\nUse SELECT, not SELECT *.")

    fn = skill_tool(name="sql_guide", description="SQL best practices", path=str(md))

    assert fn.__name__ == "sql_guide"
    assert fn.__doc__ == "SQL best practices"
    content = fn()
    assert "SQL Guide" in content
    assert "SELECT *" in content


def test_skill_tool_relative_path_resolves_from_cwd(tmp_path, monkeypatch):
    """A relative path is resolved against Path.cwd() at call time."""
    from apx_agent._tool_config import skill_tool

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "analysis.md").write_text("## Analysis\nStep 1.")

    monkeypatch.chdir(tmp_path)

    fn = skill_tool(name="analysis", description="Analysis guide", path="skills/analysis.md")
    content = fn()
    assert "Analysis" in content
    assert "Step 1" in content


def test_skill_tool_dispatch_via_load_config_tools(tmp_path):
    """load_config_tools accepts type='skill' entries from the registry."""
    from apx_agent._tool_config import load_config_tools

    md = tmp_path / "guide.md"
    md.write_text("# SQL Guide")

    tools = load_config_tools([
        {"type": "skill", "name": "sql_guide", "description": "SQL tips", "path": str(md)},
    ])
    assert len(tools) == 1
    assert tools[0].__name__ == "sql_guide"
    assert "SQL Guide" in tools[0]()
