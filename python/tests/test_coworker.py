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
