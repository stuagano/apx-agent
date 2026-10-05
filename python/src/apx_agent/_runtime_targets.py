"""Select a host contract and validate the capabilities APX actually wires."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Literal

from ._agents import BaseAgent, LlmAgent

RuntimeTarget = Literal["responses_agent", "durable_agent_server"]


@dataclass(frozen=True)
class RuntimeRequirements:
    """Required behavior, independent of the selected server's wire format.

    Sessions means a configured history/checkpoint binding, not verified crash
    durability. Recovery and long-term memory are separate requirements.
    """

    user_identity: bool = False
    approvals: bool = False
    sessions: bool = False
    long_term_memory: bool = False
    recovery: bool = False
    streaming: bool = False


@dataclass(frozen=True)
class TargetCapability:
    supported: bool
    detail: str


@dataclass(frozen=True)
class TargetReport:
    target: str
    capabilities: dict[str, TargetCapability]
    unsatisfied: list[str]

    def require_compatible(self) -> None:
        if self.unsatisfied:
            reasons = "; ".join(f"{key}: {self.capabilities[key].detail}" for key in self.unsatisfied)
            raise ValueError(f"Target {self.target!r} cannot satisfy requirements: {reasons}")


def inspect_target(
    agent: BaseAgent,
    *,
    target: RuntimeTarget,
    requirements: RuntimeRequirements | None = None,
    checkpointer: Any | None = None,
    conversation_store: Any | None = None,
) -> TargetReport:
    """Report this APX implementation's support, without contacting services."""
    if target not in ("responses_agent", "durable_agent_server"):
        raise ValueError(f"Unknown compilation target {target!r}; choose responses_agent or durable_agent_server")
    requirements = requirements or RuntimeRequirements()
    from ._apps_authorization import infer_operation_authorization
    from ._resources import _iter_tool_fns, _iter_sub_agents
    from ._topology import _iter_child_agents

    operations = [infer_operation_authorization(fn) for fn in _iter_tool_fns(agent)]
    nodes = [agent]
    for node in nodes:
        nodes.extend(child for _, child in _iter_child_agents(node) if child not in nodes)
    declared = {
        "user_identity": any(op.requires_request_context for op in operations) or bool(list(_iter_sub_agents(agent))),
        "long_term_memory": any(getattr(node, "memory_config", None) is not None for node in nodes),
        "sessions": any(getattr(node, "session_config", None) is not None for node in nodes),
    }
    responses = target == "responses_agent"
    checkpointed = checkpointer is not None and isinstance(agent, LlmAgent)
    memory_bound = (
        getattr(agent, "_apx_memory_store", None) is not None
        and not getattr(agent, "_apx_memory_degraded", None)
    )
    capabilities = {
        "user_identity": TargetCapability(responses, "ResponsesAgent preserves its existing OBO path; the durable target currently supports app-auth only."),
        "approvals": TargetCapability(checkpointed, "Approvals require an explicit LlmAgent checkpointer and stable session; durable request-user approvals are unsupported."),
        "sessions": TargetCapability(checkpointed or (responses and conversation_store is not None), "An explicit checkpoint/history binding is required; restart persistence depends on the selected store."),
        "long_term_memory": TargetCapability(responses and memory_bound, "Finalize a ResponsesAgent with a reachable declared memory store; managed memory is not wired by the durable target yet."),
        "recovery": TargetCapability(False, "Crash recovery is not wired by these target factories yet; a checkpointer alone does not register safe recovery."),
        "streaming": TargetCapability(responses, "ResponsesAgent supports streaming; the durable target currently returns complete message results."),
    }
    unsatisfied = [f.name for f in fields(requirements) if (getattr(requirements, f.name) or declared.get(f.name)) and not capabilities[f.name].supported]
    return TargetReport(target=target, capabilities=capabilities, unsatisfied=unsatisfied)


def compile_agent(
    agent: BaseAgent,
    *,
    target: RuntimeTarget,
    model: str,
    requirements: RuntimeRequirements | None = None,
    checkpointer: Any | None = None,
    conversation_store: Any | None = None,
    service_ws: Any | None = None,
) -> Any:
    """Compile one declaration into a peer host target, rejecting unmet needs.

    ResponsesAgent returns the existing pair of named handlers. The durable
    target returns the optional SDK's FastAPI application. Credentials are
    runtime bindings; they are never accepted in a durable invocation payload.
    """
    inspect_target(
        agent, target=target, requirements=requirements, checkpointer=checkpointer,
        conversation_store=conversation_store,
    ).require_compatible()
    if target == "responses_agent":
        from ._responses_agent import compile_to_responses_agent

        return compile_to_responses_agent(agent, model=model, checkpointer=checkpointer, conversation_store=conversation_store)
    if conversation_store is not None:
        raise ValueError("durable_agent_server uses a native checkpointer; conversation_store is not wired")
    from ._durable_agent import compile_to_durable_agent_server

    return compile_to_durable_agent_server(agent, model=model, service_ws=service_ws, checkpointer=checkpointer)
