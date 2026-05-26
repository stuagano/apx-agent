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
