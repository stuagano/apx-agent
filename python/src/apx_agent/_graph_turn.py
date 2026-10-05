"""Governed graph execution shared by host targets; no transport schema here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ._budget import accrue_turn, cap_for, enforce_after_turn, enforce_before_turn
from ._mlflow_tracing import safe_span


@dataclass(frozen=True)
class GraphTurn:
    messages: list[Any]
    approval_required: Any | None
    resumed: bool


def run_graph_turn(
    graph: Any,
    agent: Any,
    messages: list[Any],
    *,
    config: dict[str, Any] | None = None,
    resume: Any | None = None,
    read_interrupt: Callable[[Any, dict[str, Any] | None], Any | None],
) -> GraphTurn:
    """Execute one turn and enforce the existing token and approval contract.

    ``config`` is present only when a checkpointer is bound. State read errors
    propagate: unknown checkpoint state must not become an empty conversation.
    Host targets own credentials, protocol conversion and conversation stores.
    """
    budget_cap = cap_for(agent)
    prior = enforce_before_turn(graph, config, budget_cap) if budget_cap is not None else 0
    pre_count = len(graph.get_state(config).values.get("messages", [])) if config else 0
    with safe_span("graph.invoke", span_type="CHAIN"):
        if resume is not None:
            if config is None:
                raise ValueError("Approval resume requires a checkpointed session")
            from langgraph.types import Command

            result = graph.invoke(Command(resume=resume), config=config)
        else:
            result = graph.invoke({"messages": messages}, **({"config": config} if config else {}))

    paused = read_interrupt(graph, config)
    if paused is not None:
        if budget_cap is not None:
            accrue_turn(graph, config, prior, result["messages"][pre_count:])
        return GraphTurn(messages=[], approval_required=paused, resumed=resume is not None)

    start = pre_count if resume is not None else pre_count + len(messages)
    new_messages = result["messages"][start:]
    if budget_cap is not None:
        enforce_after_turn(graph, config, prior, new_messages, budget_cap)
    return GraphTurn(messages=new_messages, approval_required=None, resumed=resume is not None)
