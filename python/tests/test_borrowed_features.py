"""Gate tests for the borrowed-features PRD — descoped scope (#766).

Deferred tool loading and session_budget were cut from #766: both needed real
infrastructure (a runtime tool-search router; cross-turn token accumulation)
rather than wiring, and shipped as unit-green-but-non-functional. See the
follow-up issues. What remains and is genuinely wired:

* ParallelAgent context isolation (FR-4) + its empty / no-user edge cases.
* Harness-version trace attribute (FR-5) on every predict span.
* ``apx-agent status`` / ``status --json`` surfacing the harness version (FR-6).
"""

from __future__ import annotations

import importlib.metadata
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apx_agent import ParallelAgent
from apx_agent._agents import BaseAgent
from apx_agent._audit import AuditAttrs, stamp_harness_version
from apx_agent._models import Message
from apx_agent.cli import main

INSTALLED_VERSION = importlib.metadata.version("apx-agent")


# ==========================================================================
# FR-4 — ParallelAgent context isolation (+ edge cases)
# ==========================================================================


class _RecordingAgent(BaseAgent):
    def __init__(self) -> None:
        self.received: list[Message] = []

    async def run(self, messages: list[Message], request: Any) -> str:
        self.received = list(messages)
        return "ok"


@pytest.mark.asyncio
async def test_parallel_forwards_system_and_last_user_only() -> None:
    branches = [_RecordingAgent(), _RecordingAgent()]
    convo = [
        Message(role="system", content="sys"),
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content="last"),
    ]
    await ParallelAgent(branches).run(convo, MagicMock())
    for b in branches:
        roles = [(m.role, m.content) for m in b.received]
        assert roles == [("system", "sys"), ("user", "last")], roles


@pytest.mark.asyncio
async def test_parallel_empty_input_does_not_crash() -> None:
    branches = [_RecordingAgent(), _RecordingAgent()]
    assert await ParallelAgent(branches).run([], MagicMock()) == ""
    assert all(b.received == [] for b in branches)  # branches never invoked


@pytest.mark.asyncio
async def test_parallel_no_user_message_fans_out_nothing() -> None:
    # A tail with no user turn (only assistant) must not forward the assistant
    # as though it were the triggering user input.
    branches = [_RecordingAgent(), _RecordingAgent()]
    convo = [Message(role="assistant", content="stray")]
    assert await ParallelAgent(branches).run(convo, MagicMock()) == ""
    assert all(b.received == [] for b in branches)


@pytest.mark.asyncio
async def test_parallel_system_only_forwards_system_once() -> None:
    branches = [_RecordingAgent(), _RecordingAgent()]
    convo = [Message(role="system", content="sys")]
    await ParallelAgent(branches).run(convo, MagicMock())
    for b in branches:
        assert [(m.role, m.content) for m in b.received] == [("system", "sys")]


# ==========================================================================
# FR-5 — harness version span attribute
# ==========================================================================


def test_harness_version_span_attribute() -> None:
    span = MagicMock()
    stamp_harness_version(span)
    span.set_attribute.assert_any_call(AuditAttrs.HARNESS_VERSION, INSTALLED_VERSION)


# ==========================================================================
# FR-6 — apx-agent status surfaces the harness version
# ==========================================================================


def test_status_harness_version_human() -> None:
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0
    assert f"harness: {INSTALLED_VERSION}" in result.output


def test_status_harness_version_json() -> None:
    result = CliRunner().invoke(main, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["harness_version"] == INSTALLED_VERSION
