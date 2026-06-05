"""Tests for multi-agent composition — wiring leaf agents into a workflow root."""

from __future__ import annotations

import ast

import pytest

from apx_agent._ui_edit import (
    _compose_agents,
    _ensure_apx_import,
    _parse_agent_nodes,
    _render_leaf_agent,
)

_BASE = (
    'from apx_agent import Agent, tool\n\n'
    '@tool\n'
    'def echo(message: str) -> str:\n'
    '    """Echo."""\n'
    '    return message\n\n\n'
    'agent = Agent(name="hw", instructions="root", tools=[echo])\n'
)

_LEAVES = [
    {"name": "data_agent", "tools": ["echo"], "instructions": "Fetch data."},
    {"name": "response_agent", "tools": [], "instructions": "Write the answer."},
]


def _root_expr(source: str) -> str:
    """Return the source text of the root `agent =` value."""
    tree = ast.parse(source)
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and getattr(stmt.targets[0], "id", None) == "agent":
            return ast.get_source_segment(source, stmt.value) or ""
    return ""


def _exec_agent(source: str):
    """Execute generated agent.py in a fresh namespace and return the `agent`
    object — proves the code actually RUNS (imports + instantiates), not just
    that it parses."""
    ns: dict = {}
    exec(compile(source, "agent.py", "exec"), ns)  # noqa: S102 — test-only, our own generated source
    return ns["agent"]


class TestEnsureImport:
    def test_appends_to_existing_import(self):
        out = _ensure_apx_import(_BASE, "SequentialAgent")
        assert "from apx_agent import Agent, tool, SequentialAgent" in out

    def test_noop_when_present(self):
        out = _ensure_apx_import(_BASE, "Agent")
        assert out == _BASE

    def test_adds_import_when_missing(self):
        src = "from __future__ import annotations\nagent = None\n"
        out = _ensure_apx_import(src, "Agent", "SequentialAgent")
        assert "from apx_agent import Agent, SequentialAgent" in out
        # placed after __future__
        assert out.index("__future__") < out.index("from apx_agent import")


class TestRenderLeaf:
    def test_render(self):
        line = _render_leaf_agent("data_agent", ["echo", "lookup"], "Do stuff.")
        assert line == 'data_agent = Agent(tools=[echo, lookup], instructions=\'Do stuff.\')'
        compile(line, "x.py", "exec")


class TestComposedAgentRuns:
    """Stronger than compile(): the generated module must import + instantiate."""

    @pytest.mark.parametrize("pattern", ["SequentialAgent", "ParallelAgent", "RouterAgent", "HandoffAgent"])
    def test_composition_instantiates(self, pattern):
        leaves = [
            {**leaf, "route_key": leaf["name"], "route_description": "d"}
            for leaf in _LEAVES
        ]
        agent = _exec_agent(_compose_agents(_BASE, pattern, leaves))
        assert type(agent).__name__ == pattern


class TestComposeAllPatterns:
    @pytest.mark.parametrize("pattern", ["SequentialAgent", "ParallelAgent"])
    def test_list_patterns(self, pattern):
        out = _compose_agents(_BASE, pattern, _LEAVES)
        compile(out, "agent.py", "exec")
        assert _root_expr(out) == f"{pattern}(agents=[data_agent, response_agent])"
        # leaves were created
        assert "data_agent = Agent(" in out and "response_agent = Agent(" in out
        # wrapper imported
        assert pattern in out.split("\n")[0] or f", {pattern}" in out

    def test_router_emits_tuples(self):
        leaves = [
            {"name": "billing", "tools": [], "instructions": "Billing.",
             "route_key": "billing", "route_description": "Billing questions"},
            {"name": "tech", "tools": [], "instructions": "Tech.",
             "route_key": "tech", "route_description": "Technical questions"},
        ]
        out = _compose_agents(_BASE, "RouterAgent", leaves)
        compile(out, "agent.py", "exec")
        expr = _root_expr(out)
        assert expr.startswith("RouterAgent(agents=[(")
        assert "'billing', 'Billing questions', billing" in expr
        assert "'tech', 'Technical questions', tech" in expr

    def test_handoff_emits_dict_and_start(self):
        out = _compose_agents(_BASE, "HandoffAgent", _LEAVES, start="data_agent")
        compile(out, "agent.py", "exec")
        expr = _root_expr(out)
        assert "HandoffAgent(agents={" in expr
        assert "'data_agent': data_agent" in expr and "'response_agent': response_agent" in expr
        assert "start='data_agent'" in expr

    def test_handoff_defaults_start_to_first(self):
        out = _compose_agents(_BASE, "HandoffAgent", _LEAVES)
        assert "start='data_agent'" in _root_expr(out)


class TestComposeBehavior:
    def test_updates_existing_leaf_surgically(self):
        # data_agent already exists with other args — compose should patch, not clobber.
        src = (
            'from apx_agent import Agent\n'
            'data_agent = Agent(name="d", tools=[echo], instructions="old")\n'
            'response_agent = Agent(instructions="r", tools=[])\n'
            'agent = Agent(instructions="root", tools=[echo])\n'
        )
        out = _compose_agents(src, "SequentialAgent", [
            {"name": "data_agent", "tools": ["echo"], "instructions": "new data"},
            {"name": "response_agent", "tools": [], "instructions": "resp"},
        ])
        compile(out, "agent.py", "exec")
        assert 'name="d"' in out  # existing kwarg preserved
        assert "new data" in out
        assert _root_expr(out) == "SequentialAgent(agents=[data_agent, response_agent])"

    def test_round_trips_through_parser(self):
        out = _compose_agents(_BASE, "SequentialAgent", _LEAVES)
        nodes = {n["name"] for n in _parse_agent_nodes(out)}
        # The leaf agents are now parseable nodes.
        assert "data_agent" in nodes and "response_agent" in nodes

    def test_parser_reports_workflow_root_and_members(self):
        out = _compose_agents(_BASE, "SequentialAgent", _LEAVES)
        root = next(n for n in _parse_agent_nodes(out) if n["name"] == "agent")
        assert root["wrapper"] == "SequentialAgent"
        assert root["members"] == ["data_agent", "response_agent"]

    def test_parser_extracts_router_members_from_tuples(self):
        leaves = [
            {"name": "billing", "tools": [], "instructions": "b", "route_key": "billing", "route_description": "x"},
            {"name": "tech", "tools": [], "instructions": "t", "route_key": "tech", "route_description": "y"},
        ]
        out = _compose_agents(_BASE, "RouterAgent", leaves)
        root = next(n for n in _parse_agent_nodes(out) if n["name"] == "agent")
        assert root["wrapper"] == "RouterAgent"
        assert root["members"] == ["billing", "tech"]

    def test_parser_extracts_handoff_members_from_dict(self):
        out = _compose_agents(_BASE, "HandoffAgent", _LEAVES)
        root = next(n for n in _parse_agent_nodes(out) if n["name"] == "agent")
        assert root["wrapper"] == "HandoffAgent"
        assert set(root["members"]) == {"data_agent", "response_agent"}

    def test_rejects_fewer_than_two(self):
        with pytest.raises(ValueError, match="at least two"):
            _compose_agents(_BASE, "SequentialAgent", [_LEAVES[0]])

    def test_rejects_leaf_named_agent(self):
        with pytest.raises(ValueError, match="cannot be named 'agent'"):
            _compose_agents(_BASE, "SequentialAgent", [
                {"name": "agent", "tools": [], "instructions": "x"},
                {"name": "other", "tools": [], "instructions": "y"},
            ])

    def test_rejects_unknown_pattern(self):
        with pytest.raises(ValueError, match="Unsupported composition pattern"):
            _compose_agents(_BASE, "MegaAgent", _LEAVES)


class TestEditOnComposed:
    """Tool add/delete must work against a composed agent (root is a wrapper)."""

    def test_splice_attaches_to_a_chosen_leaf(self):
        from apx_agent._ui_edit import _splice_tool

        composed = _compose_agents(_BASE, "SequentialAgent", _LEAVES)
        fn = '@tool\ndef add(a: int, b: int) -> int:\n    """Add."""\n    return a + b'
        out = _splice_tool(composed, fn, "add", target="data_agent")
        agent = _exec_agent(out)  # still runs
        assert type(agent).__name__ == "SequentialAgent"
        nodes = {n["name"]: n for n in _parse_agent_nodes(out)}
        assert "add" in nodes["data_agent"]["tools"]
        # NOT wired into the wrapper root.
        assert "tools=" not in _root_expr(out)

    def test_remove_drops_tool_from_the_leaf(self):
        from apx_agent._ui_edit import _remove_tool

        composed = _compose_agents(_BASE, "SequentialAgent", _LEAVES)  # data_agent has echo
        out = _remove_tool(composed, "echo")
        compile(out, "agent.py", "exec")
        nodes = {n["name"]: n for n in _parse_agent_nodes(out)}
        assert "echo" not in nodes["data_agent"]["tools"]
        assert "def echo(" not in out  # function gone, no dangling reference


# ---------------------------------------------------------------------------
# DataAgent / CoworkerAgent in the Setup UI pattern switch
# ---------------------------------------------------------------------------

_COWORKER_SOURCE = (
    'from apx_agent.coworker import CoworkerAgent\n\n'
    'agent = CoworkerAgent("sales", "main", "sales", instructions="You are a sales assistant.")\n'
)

_DATA_AGENT_SOURCE = (
    'from apx_agent import DataAgent\n\n'
    'agent = DataAgent("analytics", "main", "sales", instructions="Query data.")\n'
)


class TestSetupPatternLeafNoOp:
    """setup_pattern must not rewrite DataAgent/CoworkerAgent positional args.

    _parse_agent_nodes uses wrapper=None for direct leaf calls (Agent, DataAgent,
    CoworkerAgent). _dev.py does `wrapper or "Agent"` as a fallback. This means
    `current_type` is always "Agent" for leaf agents, and _set_agent_wrapper is
    never called on DataAgent/CoworkerAgent source — which is correct, because
    _set_agent_wrapper only accepts Agent/LlmAgent/LoopAgent and would corrupt
    DataAgent("name","catalog","schema") → Agent("name","catalog","schema").
    """

    def test_set_agent_wrapper_on_agent_only_accepts_leaf_types(self):
        """_set_agent_wrapper rejects DataAgent/CoworkerAgent as wrapper targets."""
        from apx_agent._ui_edit import _set_agent_wrapper

        with pytest.raises(ValueError, match="Unsupported"):
            _set_agent_wrapper(_COWORKER_SOURCE, "DataAgent", target="agent")

    def test_parse_agent_nodes_identifies_coworker_as_leaf(self):
        """CoworkerAgent direct calls set wrapper=None (direct-call path)."""
        nodes = {n["name"]: n for n in _parse_agent_nodes(_COWORKER_SOURCE)}
        assert "agent" in nodes
        agent_node = nodes["agent"]
        # Direct leaf call → wrapper=None (not a composition wrapper).
        assert agent_node["wrapper"] is None
        assert agent_node.get("members") is None

    def test_parse_agent_nodes_identifies_data_agent_as_leaf(self):
        """DataAgent direct calls set wrapper=None (direct-call path)."""
        nodes = {n["name"]: n for n in _parse_agent_nodes(_DATA_AGENT_SOURCE)}
        agent_node = nodes["agent"]
        assert agent_node["wrapper"] is None
        assert agent_node.get("members") is None


class TestSetupPatternEndpoint:
    """HTTP-level regression: setup_pattern must accept DataAgent/CoworkerAgent
    and return changed=False when switching between leaf agent types."""

    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        import fastapi
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._dev import build_dev_ui_router
        from apx_agent._models import AgentCard
        import apx_agent._dev as _dev_mod
        import apx_agent._ui_edit as _edit_mod

        agent_py = tmp_path / "agent.py"
        agent_py.write_text(_COWORKER_SOURCE)
        # Patch both the module-level binding in _dev and the original in _ui_edit.
        monkeypatch.setattr(_dev_mod, "_find_agent_router_path", lambda: agent_py)
        monkeypatch.setattr(_edit_mod, "_find_agent_router_path", lambda: agent_py)

        config = AgentConfig(name="test", model="fake")
        card = AgentCard(name="test", description="", skills=[])
        ctx = AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]
        a = fastapi.FastAPI()
        a.state.agent_context = ctx
        a.include_router(build_dev_ui_router())
        return a

    @pytest.mark.asyncio
    async def test_coworker_to_agent_is_noop(self, app, tmp_path):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.post("/_apx/setup/agent-pattern", json={"pattern": "Agent"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["changed"] is False  # must NOT rewrite the file

    @pytest.mark.asyncio
    async def test_coworker_to_data_agent_is_noop(self, app):
        """DataAgent/CoworkerAgent are now in _AUTO_PATTERNS — no 'Unknown pattern' error,
        and the request is a no-op because both are leaf types."""
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.post("/_apx/setup/agent-pattern", json={"pattern": "DataAgent"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["changed"] is False  # leaf→leaf, no rewrite

    @pytest.mark.asyncio
    async def test_coworker_pattern_unknown_rejected(self, app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.post("/_apx/setup/agent-pattern", json={"pattern": "MegaAgent"})
        assert r.status_code == 400
        assert "Unknown pattern" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_coworker_to_loop_agent_falls_through(self, app, tmp_path):
        """CoworkerAgent → LoopAgent should attempt the rewrite (not be a no-op)."""
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.post("/_apx/setup/agent-pattern", json={"pattern": "LoopAgent"})
        # Either succeeds with a rewrite or 400 from compile check — but NOT a silent no-op.
        assert r.status_code in (200, 400)
        data = r.json()
        if r.status_code == 200:
            assert data.get("changed") is not False or data.get("type") == "LoopAgent"
