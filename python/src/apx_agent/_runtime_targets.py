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
    user_identity_required: bool = False
    streaming_buffered: bool = False
    recovery_enabled: bool = False

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
    # The durable target auto-attaches a managed session (AgentConfig default),
    # but a managed store currently binds only to LlmAgent. Composite agents
    # (routers/pipelines) run durable statelessly-per-request — drop the
    # auto-derived store rather than refuse to compile, and don't let it count
    # as a declared session requirement below. An explicit session_store= for a
    # composite agent still errors below (it's a real caller mistake).
    effective_session = session if isinstance(agent, LlmAgent) else None
    if effective_session is not None and effective_session.type == "managed" and checkpointer is None:
        if session_store is not None and session_store != effective_session.store_name:
            raise ValueError("session_store conflicts with the declared session.store_name")
        session_store = effective_session.store_name
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

    tools = list(_iter_tool_fns(agent))
    operations = [infer_operation_authorization(fn) for fn in tools]
    from ._defaults import _get_request, get_databricks_headers
    from ._inspection import _tool_dependency_callables

    raw_request_required = any(
        dependency in {_get_request, get_databricks_headers}
        for fn in tools for dependency in _tool_dependency_callables(fn).values()
    ) or bool(list(_iter_sub_agents(agent)))
    nodes = [agent]
    for node in nodes:
        nodes.extend(child for _, child in _iter_child_agents(node) if child not in nodes)
    declared = {
        "user_identity": any(op.requires_request_context for op in operations) or bool(list(_iter_sub_agents(agent))),
        "long_term_memory": (config is not None and config.memory is not None) or any(getattr(node, "memory_config", None) is not None for node in nodes),
        "sessions": effective_session is not None or any(getattr(node, "session_config", None) is not None for node in nodes),
    }
    responses = target == "responses_agent"
    checkpointed = (checkpointer is not None or session_store is not None) and isinstance(agent, LlmAgent)
    memory_bound = (
        getattr(agent, "_apx_memory_store", None) is not None
        and not getattr(agent, "_apx_memory_degraded", None)
    )
    if not responses:
        memory_nodes = [node for node in nodes if getattr(node, "memory_config", None) is not None
                        or getattr(node, "_apx_memory_store", None) is not None
                        or (node is agent and config is not None and config.memory is not None)]
        memory_bound = bool(memory_nodes) and all(
            getattr(node, "_apx_memory_store", None) is not None
            and not getattr(node, "_apx_memory_degraded", None) for node in memory_nodes
        )
    buffered_output = any(
        getattr(node, "_output_guardrails", None) or getattr(node, "_output_schema", None)
        or getattr(node, "_after_model", None) or getattr(node, "_after_agent_callback", None)
        for node in nodes
    )
    from ._compile import _agent_needs_node_wrap

    durable_checkpoint = session_store is not None or (
        checkpointer is not None
        and type(checkpointer).__module__ == "databricks_agentkit.langgraph.session_store"
        and type(checkpointer).__name__ == "DatabricksSessionStoreSaver"
    )
    needs_user = requirements.user_identity or declared["user_identity"] or declared["long_term_memory"]
    recovery_supported = (
        not responses and durable_checkpoint and isinstance(agent, LlmAgent)
        and not needs_user and not _agent_needs_node_wrap(agent)
        and agent._timeout_s is None
    )
    capabilities = {
        "user_identity": TargetCapability(responses or not raw_request_required, "The durable target supports SDK request-user clients, SQL and principal dependencies; raw headers, Request and remote OBO forwarding remain unsupported."),
        "approvals": TargetCapability(checkpointed, "Approvals require an LlmAgent checkpoint binding and stable session; native request-user sessions are isolated by the SDK and authenticated principal."),
        "sessions": TargetCapability(checkpointed or (responses and conversation_store is not None), "An explicit checkpoint/history binding is required; restart persistence depends on the selected store."),
        "long_term_memory": TargetCapability(memory_bound, "Bind a reachable declared store; managed memory uses AgentKit workspace memory stores and trusted caller actor IDs."),
        "recovery": TargetCapability(recovery_supported, "Opt-in recovery requires a managed Session Store, service identity and an unwrapped LlmAgent. Tools and callbacks must tolerate replay of uncommitted work; request-user recovery is unsupported by the SDK."),
        "streaming": TargetCapability(True, "Native events are persisted: incremental message deltas normally, or a validated final message when output checks require buffering."),
    }
    recovery_enabled = requirements.recovery or (config is not None and config.recovery)
    declared["recovery"] = recovery_enabled
    unsatisfied = [f.name for f in fields(requirements) if (getattr(requirements, f.name) or declared.get(f.name)) and not capabilities[f.name].supported]
    return TargetReport(target=target, capabilities=capabilities, unsatisfied=unsatisfied, session_store=session_store,
                        user_identity_required=requirements.user_identity or declared["user_identity"],
                        streaming_buffered=not responses and buffered_output,
                        recovery_enabled=recovery_enabled)


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
            preflight = inspect_target(agent, config=config, target=target, requirements=requirements,
                                       checkpointer=checkpointer, conversation_store=conversation_store,
                                       session_store=session_store)
            # Memory is bound by finalize_agent below, then checked again. All
            # other unsupported contracts must fail before binding side effects.
            if any(name != "long_term_memory" for name in preflight.unsatisfied):
                preflight.require_compatible()
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

    return compile_to_durable_agent_server(agent, model=model, service_ws=service_ws, checkpointer=checkpointer,
                                           session_store=report.session_store, require_user=report.user_identity_required,
                                           recovery=report.recovery_enabled)
