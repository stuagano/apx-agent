"""Tests for _set_agent_instructions — Setup's NL instructions now write agent.py
(the editor's + runtime's source of truth), not the ignored pyproject field."""

from __future__ import annotations

import ast

import pytest

from apx_agent._ui_edit import _set_agent_instructions


def _instructions_of(source: str, target: str = "agent") -> str:
    """Re-parse and read back the target agent's instructions= value."""
    tree = ast.parse(source)

    def find(value):
        if isinstance(value, ast.Call):
            fn = value.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in ("Agent", "LlmAgent"):
                return value
            for a in value.args:
                inner = find(a)
                if inner is not None:
                    return inner
        return None

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and getattr(stmt.targets[0], "id", None) == target:
            call = find(stmt.value)
            kw = next((k for k in call.keywords if k.arg == "instructions"), None)
            return kw.value.value if kw else None
    return None


class TestReplace:
    def test_replaces_existing_instructions(self):
        src = (
            'from apx_agent import Agent, tool\n'
            'agent = Agent(name="hw", instructions="You are a helpful assistant.", tools=[echo])\n'
        )
        out = _set_agent_instructions(src, "You are a billing expert.")
        assert _instructions_of(out) == "You are a billing expert."
        # other args preserved
        assert 'name="hw"' in out and "tools=[echo]" in out

    def test_result_is_valid_python(self):
        src = 'agent = Agent(instructions="old", tools=[t])\n'
        out = _set_agent_instructions(src, "new")
        compile(out, "agent.py", "exec")  # must not raise

    def test_multiline_instructions_round_trip(self):
        src = 'agent = Agent(instructions="x", tools=[t])\n'
        text = "Line one.\nLine two.\nLine three."
        out = _set_agent_instructions(src, text)
        assert _instructions_of(out) == text

    def test_handles_quotes_and_backslashes(self):
        src = 'agent = Agent(instructions="x", tools=[t])\n'
        nasty = 'He said "hi"\tand C:\\path'
        out = _set_agent_instructions(src, nasty)
        compile(out, "agent.py", "exec")
        assert _instructions_of(out) == nasty

    def test_wrapped_agent_inner_call(self):
        src = (
            'agent = LoopAgent(Agent(instructions="inner", tools=[t]), max_iterations=3)\n'
        )
        out = _set_agent_instructions(src, "updated inner")
        assert _instructions_of(out) == "updated inner"
        assert "LoopAgent(" in out and "max_iterations=3" in out

    def test_only_target_agent_changes(self):
        src = (
            'helper = Agent(instructions="helper", tools=[a])\n'
            'agent = Agent(instructions="root", tools=[b])\n'
        )
        out = _set_agent_instructions(src, "new root")
        assert _instructions_of(out, "agent") == "new root"
        assert _instructions_of(out, "helper") == "helper"  # untouched


class TestInsert:
    def test_inserts_when_absent(self):
        src = 'agent = Agent(tools=[echo])\n'
        out = _set_agent_instructions(src, "be helpful")
        compile(out, "agent.py", "exec")
        assert _instructions_of(out) == "be helpful"
        assert "tools=[echo]" in out

    def test_inserts_into_empty_call(self):
        src = 'agent = Agent()\n'
        out = _set_agent_instructions(src, "hi")
        compile(out, "agent.py", "exec")
        assert _instructions_of(out) == "hi"


class TestErrors:
    def test_missing_target_raises(self):
        src = 'x = 1\n'
        with pytest.raises(ValueError, match="Could not find"):
            _set_agent_instructions(src, "nope")
