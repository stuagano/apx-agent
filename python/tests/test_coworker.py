"""Tests for the coworker template — memory knob, CoworkerAgent, CoworkerTemplate."""
from __future__ import annotations

import pytest

from apx_agent.coworker import normalize_memory_knob


class TestNormalizeMemoryKnob:
    def test_off_disables_both(self):
        assert normalize_memory_knob("off") == (None, None)

    def test_inmemory_and_alias_local(self):
        for v in ("inmemory", "local", "InMemory", " LOCAL "):
            mem, sess = normalize_memory_knob(v)
            assert mem.type == "inmemory" and sess.type == "inmemory"

    def test_persistent_and_alias_delta_default_tier(self):
        for v in ("persistent", "delta"):
            mem, sess = normalize_memory_knob(v)
            assert mem.type == "delta" and sess.type == "delta"

    def test_lakebase_errors_to_explicit_block(self):
        with pytest.raises(ValueError, match="lakebase"):
            normalize_memory_knob("lakebase")

    def test_unknown_value_errors_with_valid_rungs(self):
        with pytest.raises(ValueError, match="off|inmemory|persistent"):
            normalize_memory_knob("sometimes")


class TestCoworkerAgent:
    def test_is_data_agent_with_persona_and_memory_config(self):
        from apx_agent.coworker import CoworkerAgent
        from apx_agent import DataAgent
        cw = CoworkerAgent(
            "samples", "tpch",
            persona="a revenue analyst",
            memory="persistent",
            tables={"customer": ["c_custkey(bigint)"]},
        )
        assert isinstance(cw, DataAgent)
        # persona + grounding in the instructions
        assert cw._instructions.startswith("You are a revenue analyst.")
        assert "c_custkey(bigint)" in cw._instructions
        # memory declared (not yet built — needs ws at wiring time)
        assert cw.memory_config is not None and cw.memory_config.type == "delta"
        assert cw.session_config is not None and cw.session_config.type == "delta"

    def test_memory_off_declares_nothing(self):
        from apx_agent.coworker import CoworkerAgent
        cw = CoworkerAgent("samples", "tpch", memory="off",
                           tables={"t": ["a(int)"]})
        assert cw.memory_config is None and cw.session_config is None

    def test_default_memory_is_persistent(self):
        from apx_agent.coworker import CoworkerAgent
        cw = CoworkerAgent("samples", "tpch", tables={"t": ["a(int)"]})
        assert cw.memory_config.type == "delta"
