"""Select a host contract and validate the capabilities APX actually wires."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from ._agents import BaseAgent, LlmAgent
from ._models import AgentConfig, RuntimeTarget


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
    session_store: str | None = None

    def require_compatible(self) -> None:
        if self.unsatisfied:
            reasons = "; ".join(f"{key}: {self.capabilities[key].detail}" for key in self.unsatisfied)
            raise ValueError(f"Target {self.target!r} cannot satisfy requirements: {reasons}")


def inspect_target(
    agent: BaseAgent,
    *,
    target: RuntimeTarget | None = None,
    config: AgentConfig | None = None,
    requirements: RuntimeRequirements | None = None,
    checkpointer: Any | None = None,
    conversation_store: Any | None = None,
    session_store: str | None = None,
) -> TargetReport:
    """Report this APX implementation's support, without contacting services."""
    if target is None:
        target = config.target if config is not None else "responses_agent"
    session = config.session if config is not None and config.session is not None else getattr(agent, "session_config", None)
    if session is not None and session.type == "managed" and checkpointer is None:
        if session_store is not None and session_store != session.store_name:
            raise ValueError("session_store conflicts with the declared session.store_name")
        session_store = session.store_name
    if config is not None and config.session is not None and config.session.type == "managed" and checkpointer is not None:
        raise ValueError("Declared managed sessions cannot be combined with an explicit checkpointer")
    if target not in ("responses_agent", "durable_agent_server"):
        raise ValueError(f"Unknown compilation target {target!r}; choose responses_agent or durable_agent_server")
    requirements = requirements or RuntimeRequirements()
    if session_store is not None:
        if not isinstance(session_store, str) or not session_store.strip():
            raise ValueError("session_store must be a non-empty managed store name")
        if target != "durable_agent_server" or checkpointer is not None:
            raise ValueError("session_store requires durable_agent_server and cannot be combined with checkpointer")
        if not isinstance(agent, LlmAgent):
            raise ValueError("session_store currently requires LlmAgent")
    from ._apps_authorization import infer_operation_authorization
    from ._resources import _iter_tool_fns, _iter_sub_agents
    from ._topology import _iter_child_agents

    operations = [infer_operation_authorization(fn) for fn in _iter_tool_fns(agent)]
    nodes = [agent]
    for node in nodes:
        nodes.extend(child for _, child in _iter_child_agents(node) if child not in nodes)
    declared = {
        "user_identity": any(op.requires_request_context for op in operations) or bool(list(_iter_sub_agents(agent))),
        "long_term_memory": (config is not None and config.memory is not None) or any(getattr(node, "memory_config", None) is not None for node in nodes),
        "sessions": session is not None or any(getattr(node, "session_config", None) is not None for node in nodes),
    }
    responses = target == "responses_agent"
    checkpointed = (checkpointer is not None or session_store is not None) and isinstance(agent, LlmAgent)
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
    return TargetReport(target=target, capabilities=capabilities, unsatisfied=unsatisfied, session_store=session_store)


def compile_agent(
    agent: BaseAgent,
    *,
    target: RuntimeTarget | None = None,
    model: str | None = None,
    config: AgentConfig | None = None,
    requirements: RuntimeRequirements | None = None,
    checkpointer: Any | None = None,
    conversation_store: Any | None = None,
    service_ws: Any | None = None,
    session_store: str | None = None,
) -> Any:
    """Compile one declaration into a peer host target, rejecting unmet needs.

    ResponsesAgent returns a native MLflow ResponsesAgent model. The durable
    target returns the optional SDK's FastAPI application. Credentials are
    runtime bindings; they are never accepted in a durable invocation payload.
    """
    if target is None:
        target = config.target if config is not None else "responses_agent"
    model = model if model is not None else (config.model if config is not None else None)
    if not model:
        raise ValueError("compile_agent requires model or AgentConfig.model")
    if config is not None:
        if target == "durable_agent_server":
            inspect_target(agent, config=config, target=target, requirements=requirements,
                           checkpointer=checkpointer, conversation_store=conversation_store,
                           session_store=session_store).require_compatible()
        from ._wiring import finalize_agent

        finalize_agent(agent, config, ws=service_ws)
        if target == "responses_agent" and config.session is not None:
            from ._memory_wiring import resolve_checkpointer, resolve_conversation_store

            if checkpointer is None:
                checkpointer = resolve_checkpointer(config, service_ws, agent, store_override=conversation_store)
            conversation_store = resolve_conversation_store(config, service_ws, override=conversation_store, agent=agent)
    report = inspect_target(
        agent, target=target, config=config, requirements=requirements, checkpointer=checkpointer,
        conversation_store=conversation_store, session_store=session_store,
    )
    report.require_compatible()
    if target == "responses_agent":
        from ._mlflow_model import ApxResponsesAgent

        return ApxResponsesAgent(agent, model=model, checkpointer=checkpointer, conversation_store=conversation_store)
    if conversation_store is not None:
        raise ValueError("durable_agent_server uses a native checkpointer; conversation_store is not wired")
    from ._durable_agent import compile_to_durable_agent_server

    return compile_to_durable_agent_server(agent, model=model, service_ws=service_ws, checkpointer=checkpointer, session_store=report.session_store)
