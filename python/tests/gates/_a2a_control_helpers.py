"""Shared harness for the A2A control-result gate tests.

Builds a compiled loop/handoff/sequential graph whose leaf is a declaratively
bound remote peer, and scripts that peer's control replies by patching
``RemoteDatabricksAgent.run_with_control`` — the single seam the orchestrator
uses. No live workspace, no model: a pure-remote loop/handoff node needs no
local LLM, matching the branch's local-ASGI/mocked-SDK convention.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from apx_agent import (
    Agent,
    AgentConfig,
    BaseAgent,
    HandoffAgent,
    LoopAgent,
    SequentialAgent,
    compile_to_langgraph,
    finalize_agent,
)
from apx_agent._a2a_models import ControlSignal
from apx_agent._remote import _RemoteReply, RemoteDatabricksAgent

CARD_URL = "https://pricing.internal/.well-known/agent.json"


def reply(text: str, control: ControlSignal | None = None) -> _RemoteReply:
    return _RemoteReply(text=text, control=control)


def finish_loop_control() -> ControlSignal:
    return ControlSignal(name=LoopAgent.FINISH_TOOL, args={}, id="c1")


def transfer_control(target: str, **args: Any) -> ControlSignal:
    return ControlSignal(name=f"{HandoffAgent.TRANSFER_PREFIX}{target}", args=args, id="t1")


def patch_remote_replies(
    monkeypatch: pytest.MonkeyPatch, replies: Sequence[_RemoteReply]
) -> list[Any]:
    """Script the bound peer's control replies; record posted messages.

    Each call to the peer pops the next reply; the last reply repeats once the
    script is exhausted (so a "continue" loop still terminates at max_iter).
    """
    calls: list[Any] = []
    iterator: Iterator[_RemoteReply] = iter(replies)
    last = replies[-1]

    async def _fake(
        self: RemoteDatabricksAgent, messages: Any, incoming_headers: Any
    ) -> _RemoteReply:
        calls.append(list(messages))
        return next(iterator, last)

    monkeypatch.setattr(RemoteDatabricksAgent, "run_with_control", _fake)
    return calls


def _finalize(root: BaseAgent, tmp_path: Path) -> None:
    config = AgentConfig(name="ctrl-test", bindings={"body": "$BODY_URL"})
    finalize_agent(root, config, pyproject_path=str(tmp_path / "missing.toml"))


def build_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, max_iterations: int = 3) -> Any:
    monkeypatch.setenv("BODY_URL", CARD_URL)
    root = LoopAgent(Agent(name="body"), max_iterations=max_iterations)
    _finalize(root, tmp_path)
    return compile_to_langgraph(root, ws=None, model="unused-model")


def build_handoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Handoff whose start peer ('body') is remote and transfers to 'target'.

    'target' is also a bound remote leaf so no local model is needed; only the
    routing decision under test matters.
    """
    monkeypatch.setenv("BODY_URL", CARD_URL)
    monkeypatch.setenv("TARGET_URL", CARD_URL)
    root = HandoffAgent(agents=[Agent(name="body"), Agent(name="target")])
    config = AgentConfig(
        name="ctrl-test", bindings={"body": "$BODY_URL", "target": "$TARGET_URL"}
    )
    finalize_agent(root, config, pyproject_path=str(tmp_path / "missing.toml"))
    return compile_to_langgraph(root, ws=None, model="unused-model")


def build_sequential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("BODY_URL", CARD_URL)
    root = SequentialAgent([Agent(name="body")], name="seq")
    _finalize(root, tmp_path)
    return compile_to_langgraph(root, ws=None, model="unused-model")


def run(graph: Any, query: str = "go") -> dict[str, Any]:
    return graph.invoke({"messages": [HumanMessage(content=query)]})
