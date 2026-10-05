"""Governed graph execution shared by host targets; no transport schema here."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
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
    emit_message: Callable[[Any], None] | None = None,
    invocation_id: str | None = None,
    recovery: bool = False,
) -> GraphTurn:
    """Execute one turn and enforce the existing token and approval contract.

    ``config`` is present only when a checkpointer is bound. State read errors
    propagate: unknown checkpoint state must not become an empty conversation.
    Host targets own credentials, protocol conversion and conversation stores.
    """
    budget_cap = cap_for(agent)
    snapshot = graph.get_state(config) if config else None
    pre_count = len(snapshot.values.get("messages", [])) if snapshot else 0
    continuing = False
    if invocation_id is not None:
        if config is None:
            raise ValueError("Recovery requires a checkpoint binding")
        digest = hashlib.sha256(json.dumps(
            {"messages": [m.model_dump(mode="json") for m in messages], "resume": resume},
            sort_keys=True,
        ).encode()).hexdigest()
        saved_record = (snapshot.metadata or {}).get("apx_turn") if snapshot else None
        record = json.loads(saved_record) if saved_record is not None else None
        continuing = bool(record and record["invocation_id"] == invocation_id)
        if record is not None and continuing:
            if record["input_digest"] != digest:
                raise ValueError("Recovery input differs from the checkpointed invocation")
            pre_count, prior = record["pre_count"], record["prior_tokens"]
        else:
            if recovery and any(
                (state.metadata or {}).get("databricks_agentkit.invocation_id") == invocation_id
                for state in graph.get_state_history(config)
            ):
                raise ValueError("Cannot recover an invocation behind a newer session checkpoint")
            prior = int((snapshot.values.get("state") or {}).get("session_tokens", 0)) if snapshot else 0
            record = {"invocation_id": invocation_id, "input_digest": digest,
                      "pre_count": pre_count, "prior_tokens": prior}
        config = {**config, "metadata": {
            # LangGraph checkpoint metadata retains scalars, not nested dicts.
            **config.get("metadata", {}), "apx_turn": json.dumps(record, sort_keys=True),
            "databricks_agentkit.invocation_id": invocation_id,
        }}
        if budget_cap is not None and prior >= budget_cap:
            from ._errors import SessionBudgetExceeded

            raise SessionBudgetExceeded(spent=prior, cap=budget_cap)
    else:
        if recovery:
            raise ValueError("Recovery requires an invocation ID")
        prior = enforce_before_turn(graph, config, budget_cap) if budget_cap is not None else 0
    with safe_span("graph.invoke", span_type="CHAIN"):
        if continuing:
            graph_input: Any = None
        elif resume is not None:
            if config is None:
                raise ValueError("Approval resume requires a checkpointed session")
            from langgraph.types import Command

            graph_input = Command(resume=resume)
        else:
            graph_input = {"messages": messages}
        if continuing and snapshot is not None and not snapshot.next:
            result = snapshot.values
        elif emit_message is None:
            result = graph.invoke(graph_input, **({"config": config} if config else {}))
        else:
            result = None
            for mode, value in graph.stream(
                graph_input, stream_mode=["messages", "values"],
                **({"config": config} if config else {}),
            ):
                if mode == "messages":
                    emit_message(value[0])
                elif mode == "values":
                    result = value
            if result is None:
                raise RuntimeError("Graph stream completed without a state result")

    paused = read_interrupt(graph, config)
    if paused is not None:
        if budget_cap is not None:
            accrue_turn(graph, config, prior, result["messages"][pre_count:], strict=invocation_id is not None)
        return GraphTurn(messages=[], approval_required=paused, resumed=resume is not None)

    start = pre_count if resume is not None else pre_count + len(messages)
    new_messages = result["messages"][start:]
    if budget_cap is not None:
        enforce_after_turn(graph, config, prior, new_messages, budget_cap, strict=invocation_id is not None)
    return GraphTurn(messages=new_messages, approval_required=None, resumed=resume is not None)
