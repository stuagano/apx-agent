"""Gate tests for the borrowed-features PRD — shipped scope (#766).

Three of the four candidate features were cut from #766: they targeted the
direct ``.run()`` path but production serves through the compiled LangGraph, so
they were unit-green but non-functional in a deployed agent. Follow-ups:

* deferred tool loading → #767 (needs a runtime tool_search router)
* session_budget → #768 (needs cross-turn accumulation on served paths)
* ParallelAgent context isolation → #769 (must be applied in the compiled
  StateGraph fan-out, not ``ParallelAgent.run``)

What ships here is the one feature genuinely wired into every served predict
path: the harness-version trace attribute (FR-5) and its CLI surfacing (FR-6).
"""

from __future__ import annotations

import importlib.metadata
import json
from unittest.mock import MagicMock

from click.testing import CliRunner

from apx_agent._audit import AuditAttrs, stamp_harness_version
from apx_agent.cli import main

INSTALLED_VERSION = importlib.metadata.version("apx-agent")


# FR-5 — harness version stamped on every predict / predict_stream span
def test_harness_version_span_attribute() -> None:
    span = MagicMock()
    stamp_harness_version(span)
    span.set_attribute.assert_any_call(AuditAttrs.HARNESS_VERSION, INSTALLED_VERSION)


# FR-6 — apx-agent status surfaces the harness version (human + JSON)
def test_status_harness_version_human() -> None:
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0
    assert f"harness: {INSTALLED_VERSION}" in result.output


def test_status_harness_version_json() -> None:
    result = CliRunner().invoke(main, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["harness_version"] == INSTALLED_VERSION
