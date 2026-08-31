"""FastAPI integration — ``setup_agent()`` and ``create_app()``.

Mounts the supported Mosaic AI surface on the agent:

  * ``POST /invocations`` — MLflow ChatAgent protocol (Model Serving, Review App,
    Agent Evaluation). OBO header bridge for user-scoped auth in Apps.
  * ``POST /responses`` — MLflow ResponsesAgent protocol (AI Playground, Apps
    runtime). Same compiled agent, same conversation_store — no divergence.
  * ``GET /.well-known/agent.json`` — A2A discovery card.
  * ``GET /health`` — liveness probe.
  * ``GET|POST|DELETE /mcp`` — stateless MCP HTTP transport for Genie Code
    and AI Playground.
  * ``{api_prefix}/tools/<name>`` — per-tool FastAPI routes for direct invocation.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from starlette.responses import Response

from ._agents import BaseAgent, LlmAgent
from ._env import resolve_env_var
from ._prompt_assembly import compose_instructions
from ._defaults import _make_workspace_client
from ._inspection import _load_agent_config
from ._mcp import _build_mcp_components
from ._models import (
    A2ASkill,
    AgentCard,
    AgentConfig,
    AgentContext,
    AgentTool,
    A2AFlowGraph,
    FLOW_GRAPH_TOOL_NAME,
)

logger = logging.getLogger(__name__)


def _dedupe_tools(tools: list[AgentTool]) -> list[AgentTool]:
    seen: set[str] = set()
    deduped: list[AgentTool] = []
    for tool in tools:
        if tool.name in seen:
            continue
        deduped.append(tool)
        seen.add(tool.name)
    return deduped


def _builtin_agent_flow_graph(agent: BaseAgent) -> LlmAgent | None:
    """Expose the live topology tool without requiring user config."""
    from ._topology import agent_flow_graph_tool

    existing = {t.name for t in agent.collect_tools()}
    if FLOW_GRAPH_TOOL_NAME in existing:
        return None

    return LlmAgent(tools=[agent_flow_graph_tool()])


def apply_config_knobs(agent: BaseAgent, config: AgentConfig) -> None:
    """Apply ``[tool.apx.agent]`` config values onto the live agent instance.

    This is the **shared config→instance seam** that both serve paths must run:

      * ``apx-agent run`` / Apps target — via ``setup_agent`` (this module).
      * model-serving deploy — via ``apx-agent deploy`` calling this right before
        ``log_agent``, because MLflow captures the agent *at log time*; nothing
        re-applies config inside the logged model's per-request compile.

    Keeping both paths on one helper is what prevents cross-target drift (a
    knob that works under ``apx-agent run`` but silently no-ops on a deploy). Future
    declarative features that likewise need to land on the instance before it
    is captured — tools merge, memory attach, guard attach — should extend this
    same function rather than re-implementing the merge at one call site.

    Semantics for the generation knobs: the compile path (``_compile.py``) reads
    ``temperature`` / ``max_tokens`` / ``max_iterations`` off the instance, not
    off config. Constructor wins — only copy when the instance left the attr at
    ``None``. Uses ``is None`` (not a truthy check) so a deliberate
    ``temperature=0.0`` / ``max_iterations=0`` isn't clobbered, and ``hasattr``
    guards composition agents (e.g. ``SequentialAgent``) that don't define
    every knob. Idempotent: a second call sees a non-``None`` attr and no-ops.

    Semantics for instructions: this ALSO overlays ``config.instructions`` onto
    ``agent._instructions`` via ``compose_instructions`` (persona above
    grounding). Unlike the generation knobs' constructor-wins *fill*, this is
    *compose* — when both the template-set grounding and the envelope persona
    are present, both are kept (overlay first, grounding below). When only one
    side is non-empty, that side is used verbatim (fill). A whitespace-only
    ``config.instructions`` is treated as empty (no-op). Idempotent per instance
    via the ``_persona_overlaid`` sentinel: a second call leaves instructions
    untouched.
    """
    for attr, config_value in (
        ("_temperature", config.temperature),
        ("_max_tokens", config.max_tokens),
        ("_max_iterations", config.max_iterations),
    ):
        if (
            config_value is not None
            and hasattr(agent, attr)
            and getattr(agent, attr) is None
        ):
            setattr(agent, attr, config_value)

    # Persona instruction overlay. The compile path reads ``agent._instructions``
    # as the system prompt. A template may have set grounded instructions; the
    # envelope may carry persona instructions. Compose (overlay above grounding)
    # when both are present; otherwise fill. Idempotent via a sentinel so a
    # second call (e.g. mount_mcp_endpoints re-running setup_agent) is a no-op.
    if config.instructions.strip():
        if hasattr(agent, "_instructions"):
            if not getattr(agent, "_persona_overlaid", False):
                # getattr/setattr (not direct attr access) because the param is
                # typed BaseAgent; _instructions/_persona_overlaid are LlmAgent
                # state — same pattern as the generation-knob loop above.
                setattr(
                    agent,
                    "_instructions",
                    compose_instructions(
                        base=getattr(agent, "_instructions"),
                        overlay=config.instructions,
                    ),
                )
                setattr(agent, "_persona_overlaid", True)
        else:
            # Composition roots (SequentialAgent/RouterAgent/...) hold no system
            # prompt of their own — instructions live on inner leaves — so the
            # persona overlay has nowhere to land. Skipping is intentional.
            logger.debug(
                "Skipping persona instruction overlay: %s is a composition root "
                "without its own system prompt.",
                type(agent).__name__,
            )


def _guardrails_configured(cfg: Any) -> bool:
    """True when ``[tool.apx.agent.guardrails]`` declares any active rule."""
    return bool(
        cfg.blocked_tools
        or cfg.allowed_tools is not None
        or cfg.rate_limit is not None
        or cfg.injection_detection
    )


def _agent_tool_nested_agents(agent: BaseAgent) -> list[BaseAgent]:
    """Return ``BaseAgent`` instances closed over by ``agent_tool`` wrappers."""
    found: list[BaseAgent] = []
    for fn in getattr(agent, "_tool_fns", []) or []:
        closure = getattr(fn, "__closure__", None)
        if not closure:
            continue
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, BaseAgent):
                found.append(value)
    return found


def _collect_guardrail_targets(agent: BaseAgent) -> list[BaseAgent]:
    """Every ``LlmAgent`` leaf that can receive config ``before_tool`` / input guards.

    Walks composition children (Sequential / Router / Handoff / …) and
    ``agent_tool``-wrapped specialists (#616). Remote peers without
    ``_before_tool`` are skipped — they enforce their own policy.
    """
    from ._topology import _iter_child_agents  # noqa: PLC0415

    targets: list[BaseAgent] = []
    seen: set[int] = set()

    def _walk(node: BaseAgent) -> None:
        nid = id(node)
        if nid in seen:
            return
        seen.add(nid)
        children = _iter_child_agents(node)
        nested = _agent_tool_nested_agents(node)
        if not children and not nested:
            if hasattr(node, "_before_tool") or hasattr(node, "_input_guardrails"):
                targets.append(node)
            return
        for _, child in children:
            _walk(child)
        for nested_agent in nested:
            _walk(nested_agent)
        # Parent LlmAgent that also has tools (orchestrator + agent_tool) still
        # needs its own gates for wrapper tool names.
        if hasattr(node, "_before_tool") or hasattr(node, "_input_guardrails"):
            if node not in targets:
                targets.append(node)

    _walk(agent)
    return targets


def _attach_config_guards_to_leaf(
    leaf: BaseAgent,
    *,
    input_guardrails: list[Any],
    before_tool: Any,
    compose: Any,
) -> bool:
    """Attach config guards onto one leaf. Returns True if anything attached."""
    if getattr(leaf, "_apx_config_guards_applied", False):
        return False
    attached = False
    if input_guardrails:
        existing_igs = getattr(leaf, "_input_guardrails", None)
        if existing_igs is not None:
            existing_igs.extend(input_guardrails)
            attached = True
    if before_tool is not None and hasattr(leaf, "_before_tool"):
        code_hook = getattr(leaf, "_before_tool", None)
        if code_hook is not None:
            setattr(leaf, "_before_tool", compose(code_hook, before_tool))
        else:
            setattr(leaf, "_before_tool", before_tool)
        attached = True
    if attached:
        setattr(leaf, "_apx_config_guards_applied", True)
    return attached


def apply_config_guardrails(agent: BaseAgent, config: AgentConfig) -> None:
    """Apply ``[tool.apx.agent.guardrails]`` config onto live agent leaf instances.

    Translates ``config.guardrails`` (a ``GuardrailsConfig``) into built-in
    guard callables and attaches them additively to every ``LlmAgent`` leaf
    in the composition tree (and ``agent_tool`` specialists) (#616):

    - ``before_tool`` gates (deny / allow / rate-limit) are merged via
      ``compose(existing_code_hook, *config_gates)`` — code hook runs first.
    - ``input_guardrails`` (injection heuristic) are appended — code guards
      run first.

    Idempotent via the ``_apx_config_guards_applied`` sentinel on the root and
    each leaf: a second call is a no-op. ``setup_agent`` can run more than once
    on the same instance (``mount_mcp_endpoints`` fires its own ``setup_agent``
    at startup), so this is a real correctness requirement, not a nicety.

    Raises ``ValueError`` when guardrails are declared but no leaf can receive
    them (composition of remotes-only, etc.) — fail loud, not warn-and-skip.
    Remote leaves without ``_before_tool`` keep their own policy; document that
    operators must configure policy on those peers separately.
    """
    if getattr(agent, "_apx_config_guards_applied", False):
        return

    from ._guards import build_config_guards, compose  # noqa: PLC0415

    cfg = config.guardrails
    configured = _guardrails_configured(cfg)
    _guards = build_config_guards(cfg)

    if not configured:
        setattr(agent, "_apx_config_guards_applied", True)
        return

    targets = _collect_guardrail_targets(agent)
    attached_any = False
    for leaf in targets:
        if _attach_config_guards_to_leaf(
            leaf,
            input_guardrails=_guards.input_guardrails,
            before_tool=_guards.before_tool,
            compose=compose,
        ):
            attached_any = True

    if not attached_any:
        raise ValueError(
            f"[tool.apx.agent.guardrails] declared on {type(agent).__name__} but "
            "no LlmAgent leaf could receive them (composition roots and remote "
            "agent_tool peers have no _before_tool / _input_guardrails). Attach "
            "guardrails on each leaf LlmAgent, or ensure the tree contains one."
        )

    setattr(agent, "_apx_config_guards_applied", True)


def _service_policies_configured(config: AgentConfig) -> bool:
    """True when local Service Policy mirroring is declared and enabled."""
    return config.service_policies.local_mode.value == "mirror" and bool(
        config.service_policies.attachments
    )


def _attach_service_policies_to_leaf(
    leaf: BaseAgent,
    adapter: Any,
    *,
    compose: Any,
) -> bool:
    """Attach one shared local policy adapter to a leaf exactly once."""
    if getattr(leaf, "_apx_service_policies_applied", False):
        return False
    attached = False
    if hasattr(leaf, "_input_guardrails"):
        getattr(leaf, "_input_guardrails").append(adapter.for_input())
        attached = True
    if hasattr(leaf, "_output_guardrails"):
        getattr(leaf, "_output_guardrails").append(adapter.for_output())
        attached = True
    if hasattr(leaf, "_before_tool"):
        current = getattr(leaf, "_before_tool", None)
        setattr(leaf, "_before_tool", compose(current, adapter.for_tool()) if current is not None else adapter.for_tool())
        attached = True
    if hasattr(leaf, "_after_tool"):
        current = getattr(leaf, "_after_tool", None)
        setattr(leaf, "_after_tool", compose(current, adapter.for_tool_result()) if current is not None else adapter.for_tool_result())
        attached = True
    if hasattr(leaf, "_before_model"):
        current = getattr(leaf, "_before_model", None)
        setattr(leaf, "_before_model", compose(current, adapter.for_model()) if current is not None else adapter.for_model())
        attached = True
    if attached:
        setattr(leaf, "_apx_service_policies_applied", True)
        setattr(leaf, "_apx_service_policy_adapter", adapter)
    return attached


def apply_config_service_policies(agent: BaseAgent, config: AgentConfig) -> None:
    """Apply local Service Policy hooks through the shared runtime seam."""
    if getattr(agent, "_apx_service_policies_applied", False):
        return
    if not _service_policies_configured(config):
        setattr(agent, "_apx_service_policies_applied", True)
        return

    from ._guards import compose  # noqa: PLC0415
    from ._service_policies_local import LocalServicePolicyAdapter  # noqa: PLC0415

    adapter = LocalServicePolicyAdapter(config.service_policies)
    targets = _collect_guardrail_targets(agent)
    attached_any = False
    for leaf in targets:
        if _attach_service_policies_to_leaf(leaf, adapter, compose=compose):
            attached_any = True
    if not attached_any:
        raise ValueError(
            f"[tool.apx.agent.service_policies] declared on {type(agent).__name__} but "
            "no eligible local agent leaf could receive policy hooks."
        )
    setattr(agent, "_apx_service_policies_applied", True)
    setattr(agent, "_apx_service_policy_adapter", adapter)


def attach_declared_vector_search(agent: BaseAgent, config: AgentConfig) -> None:
    """Wire ``[tool.apx.agent] vector_search_index`` into the agent as a tool.

    Mints a ``vector_search_tool`` for the declared index and registers it on the
    agent, so a declared index becomes a live tool in both the A2A card and the
    compiled LangGraph. Called from ``finalize_agent`` BEFORE
    ``agent.collect_tools()``, mirroring ``attach_declared_memory``.

    The tool is a pure closure — its workspace client is injected per-request and
    runs as the calling user — so no ``ws`` is needed at attach time and no boot
    probe/degraded path applies (unlike Lakebase memory).

    Guards, matching the declared-memory / ``merge_config_tools`` precedent:
    - no ``vector_search_index`` declared → no-op;
    - a composition root with no ``_register_tool`` warns and is skipped
      (attach on a leaf ``LlmAgent``);
    - a code-wired ``vector_search`` tool wins on name collision — the declared
      index is skipped. This also makes a second call a no-op (idempotent).
    """
    index_name = config.vector_search_index
    if index_name is None:
        return

    register = getattr(agent, "_register_tool", None)
    if register is None:
        logger.warning(
            "[tool.apx.agent] vector_search_index declared on a %s root that has no "
            "_register_tool — skipping (attach on a leaf LlmAgent).",
            type(agent).__name__,
        )
        return

    existing = {getattr(fn, "__name__", None) for fn in getattr(agent, "_tool_fns", [])}
    if "vector_search" in existing:
        logger.warning(
            "[tool.apx.agent] vector_search_index declares a 'vector_search' tool but "
            "the agent already wires one — keeping the code-wired tool, ignoring the "
            "declared index.",
        )
        return

    from .vector_search import vector_search_tool  # noqa: PLC0415

    register(vector_search_tool(index_name))


def finalize_agent(
    agent: BaseAgent,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
    ws: Any | None = None,
) -> None:
    """Apply all config→instance steps before the agent is served or logged.

    The single seam every runtime must run: it applies generation knobs + the
    persona instruction overlay (apply_config_knobs) AND merges
    [[tool.apx.tools]] (merge_config_tools). Idempotent — safe to call from
    setup_agent (serve), log_agent (log/deploy), and apx info; a second call is
    a no-op. Future declarative features (memory, guards — E3) extend here.

    When *config* is supplied, knobs are applied from it and *pyproject_path*
    is used only by merge_config_tools (to locate [[tool.apx.tools]]). When
    *config* is omitted, it is loaded from *pyproject_path*; if no config is
    found, knobs are skipped but the tool merge still runs.

    Note: a project with no [tool.apx.agent] section is not servable (the serve
    path requires an agent section), so finalize_agent is not invoked via the
    serve path for such a project. Whether tools-only agents should be servable
    is a future (E3) design question.
    """
    if config is None:
        config = _load_agent_config(pyproject_path=pyproject_path)
    if config is not None:
        apply_config_knobs(agent, config)
        # E3c: attach declarative guards (idempotent; warns on composition
        # roots lacking the guard hook attributes).
        apply_config_guardrails(agent, config)
        apply_config_service_policies(agent, config)

    # Local import: _tool_config lazily imports _resolve_env_var from this module;
    # a top-level import here would make that cycle unconditional at load time.
    from ._tool_config import merge_config_tools  # noqa: PLC0415

    merge_config_tools(agent, pyproject_path=pyproject_path)

    # E3b: attach config-declared memory/example tools AFTER the tool merge so
    # code-wired tools' names are already in the existing set (collision guard).
    # Must run BEFORE agent.collect_tools() (the A2A card snapshot in setup_agent)
    # so memory tools appear in the card. attach_declared_memory is idempotent.
    if config is not None:
        from ._memory_wiring import attach_declared_memory  # noqa: PLC0415

        attach_declared_memory(agent, config, ws=ws)

        # E3d: wire a config-declared Vector Search index as a tool, after the
        # tool merge + memory attach so code-wired names are already in the
        # collision set. Must run BEFORE agent.collect_tools() (the A2A card
        # snapshot) so the tool appears in the card and compiled graph.
        # Self-guards when no index is declared (like attach_declared_memory).
        attach_declared_vector_search(agent, config)

    # Late ws-binding: a DataAgent constructed at import time (the Python-canonical
    # agent.py path) has ws=None and so couldn't wire its UC-function tools. Now
    # that the live ws is available, give it one. Idempotent, so it's a no-op for
    # an agent already built with a ws.
    if ws is not None:
        from .data_agent import DataAgent  # noqa: PLC0415 — avoid import cycle

        if isinstance(agent, DataAgent):
            agent.bind_workspace(ws)


class TemplateConfigError(ValueError):
    """Raised when an agent cannot be resolved from the given template config or module."""


def _ws_for_template(config: "AgentConfig | None") -> Any:
    """Return a WorkspaceClient for template resolution, or None.

    Only attempts construction when config has a template field (template.build
    may need ws for live schema introspection). Degrades gracefully on failure —
    DataTemplate.build(spec, ws=None) still returns a working agent.
    """
    if config is None or config.template is None:
        return None
    try:
        return _make_workspace_client()
    except Exception as e:
        logger.warning(
            "Could not build workspace client for template resolution: %s. "
            "Template will build with ws=None (graceful degradation — "
            "grounded instructions require live introspection).",
            e,
        )
        return None


def resolve_agent(
    module_spec: str | None,
    config: "AgentConfig | None",
    *,
    ws: Any | None = None,
) -> "BaseAgent":
    """Resolve a ``BaseAgent`` from either a template config or a module import.

    Runs BEFORE ``finalize_agent`` (which then layers knobs/persona/tools/guards).

    **Resolution order (precedence):** ``config.template`` is checked FIRST — when
    both a ``template`` field and a ``module_spec`` are present, the template wins
    and the module import is never attempted.

    1. ``config.template`` set → ``template_registry.build(name, spec, ws=ws)``.
       ``name`` key selects the template; other keys form the spec dict.
    2. else → import ``module_spec`` (``module:variable``) via ``importlib``
       (NOT ``cli._load_agent`` — that would create a ``cli → _wiring`` cycle).
    3. neither → ``TemplateConfigError`` with a clear message.

    Note: this function is only called on the CLI/deploy paths.  On the serve path
    (``create_app``), an explicit ``agent=`` argument passed by the caller skips
    ``resolve_agent`` entirely (see the ``if agent is None`` guard), so a
    ``create_app(agent=my_agent)`` call always wins over any template config.
    """
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    from ._template import template_registry

    template_dict: dict[str, Any] | None = None
    if config is not None and config.template is not None:
        template_dict = config.template

    if template_dict is not None:
        tname = template_dict.get("name")
        if not tname:
            raise TemplateConfigError(
                "AgentConfig.template must include a 'name' key to select the template. "
                f"Got: {template_dict!r}"
            )
        spec = {k: v for k, v in template_dict.items() if k != "name"}
        if config is not None and config.knowledge is not None and "knowledge" not in spec:
            spec["knowledge"] = config.knowledge
        return template_registry.build(tname, spec, ws=ws)

    if not module_spec:
        raise TemplateConfigError(
            "No agent to resolve: config has no 'template' field and no module_spec "
            "was provided. Either add 'template = { name = \"...\", ... }' to "
            "[tool.apx.agent] or pass a 'module:variable' module_spec."
        )
    if ":" not in module_spec:
        raise TemplateConfigError(
            f"module_spec must be 'module:variable', got {module_spec!r}."
        )
    mod_path, _, var_name = module_spec.partition(":")
    if not mod_path or not var_name:
        raise TemplateConfigError(
            f"Both module and variable must be non-empty in module_spec, got {module_spec!r}."
        )
    cwd = str(_Path.cwd())
    if cwd not in _sys.path:
        _sys.path.insert(0, cwd)
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as e:
        raise TemplateConfigError(
            f"Failed to import {mod_path!r}: {e}. "
            "Make sure the module is on PYTHONPATH or in the current directory."
        ) from e
    if not hasattr(mod, var_name):
        raise TemplateConfigError(
            f"Module {mod_path!r} has no attribute {var_name!r}."
        )
    return getattr(mod, var_name)


# Re-exported under the old name: _tool_config lazily imports _resolve_env_var
# from this module. The implementation moved to ._env (shared helper, #436).
_resolve_env_var = resolve_env_var


def _mount_trace_feedback_routes(app: FastAPI) -> None:
    if getattr(app.state, "trace_feedback_routes_mounted", False):
        return
    from ._trace_feedback_api import build_trace_feedback_router

    app.include_router(build_trace_feedback_router())
    app.state.trace_feedback_routes_mounted = True


async def setup_agent(
    app: FastAPI,
    agent: "BaseAgent | None",
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
) -> AgentContext | None:
    """Wire agent protocol routes onto an existing FastAPI app.

    Mounts:
      * tool routes at ``{api_prefix}/tools/<name>``
      * ``GET /.well-known/agent.json`` (A2A discovery)
      * ``GET /health``
      * MCP transports at ``/mcp`` and ``/mcp/sse`` (when ``mcp`` extra installed)
      * Dev UI at ``/_apx/*`` (when ``_dev`` module loadable)

    The ``POST /invocations`` route is mounted separately by ``create_app``
    after ``setup_agent`` runs (depends on the eval/mlflow extra).
    Returns the ``AgentContext``, or ``None`` if config is missing.
    """
    _mount_trace_feedback_routes(app)
    if config is None:
        config = _load_agent_config(pyproject_path=pyproject_path)
    if config is None:
        logger.info("No agent config found — agent protocol disabled")
        app.state.agent_context = None
        return None

    # E3a: resolve from template if no agent was passed in.
    if agent is None:
        agent = resolve_agent(
            None,
            config,
            ws=getattr(app.state, "workspace_client", None),
        )

    # Merge sub_agents from config
    if config.sub_agents:
        if not hasattr(agent, "_sub_agent_urls"):
            # Only LlmAgent defines _sub_agent_urls; on a composition root
            # getattr(..., []) would return a throwaway list, silently dropping
            # config-declared sub_agents from the A2A/MCP discovery surface
            # (audit M7). Warn loudly instead of failing silently.
            logger.warning(
                "config sub_agents %s set on a %s root, which does not support "
                "sub-agent merging (only LlmAgent does) — these are ignored. "
                "Declare sub_agents on a leaf LlmAgent instead.",
                config.sub_agents,
                type(agent).__name__,
            )
        sub_agent_urls: list[str] = getattr(agent, "_sub_agent_urls", [])
        existing = set(sub_agent_urls)
        for raw_url in config.sub_agents:
            resolved = _resolve_env_var(raw_url)
            if not resolved:
                logger.warning(
                    f"sub_agents config: {raw_url} resolved to empty — skipping"
                )
                continue
            if resolved not in existing:
                sub_agent_urls.append(resolved)
                existing.add(resolved)

    # Apply knobs + persona overlay + config-tool merge + memory attach BEFORE
    # the card snapshot (collect_tools below) so all declared tools are both
    # callable and advertised. ws is set by the lifespan before setup_agent runs.
    finalize_agent(
        agent,
        config,
        pyproject_path=pyproject_path,
        ws=getattr(app.state, "workspace_client", None),
    )

    builtin_agent = _builtin_agent_flow_graph(agent)
    tools = _dedupe_tools(agent.collect_tools())
    if builtin_agent is not None:
        tools = _dedupe_tools([*tools, *builtin_agent.collect_tools()])
    # fetch_remote_tools ALSO materializes each reachable sub-agent as a
    # callable tool in the agent's _tool_fns (#436) — the card below and the
    # compiled graph's tool set therefore derive from the same source of
    # truth. Dedupe by name so a repeated setup on the same agent instance
    # (whose collect_tools now already includes the delegates) doesn't
    # advertise a skill twice.
    remote_tools = await agent.fetch_remote_tools()
    known_names = {t.name for t in tools}
    for tool in remote_tools:
        if tool.name not in known_names:
            tools.append(tool)
            known_names.add(tool.name)
    card = AgentCard(
        name=config.name,
        description=config.description,
        flowGraph=A2AFlowGraph(
            toolEndpoint=f"{config.api_prefix}/tools/{FLOW_GRAPH_TOOL_NAME}"
        ),
        skills=[
            A2ASkill(
                id=t.name,
                name=t.name,
                description=t.description,
                inputSchema=t.input_schema,
                outputSchema=t.output_schema,
            )
            for t in tools
        ],
    )
    from ._schema import load_baked_schema
    ctx = AgentContext(
        config=config, tools=tools, card=card, agent=agent,
        schema=load_baked_schema(),
    )
    app.state.agent_context = ctx

    logger.info(f"Agent protocol enabled: {config.name} ({len(tools)} tools)")

    from ._appkit_tool_bridge import build_appkit_tool_bridge_router

    app.include_router(build_appkit_tool_bridge_router())

    # Per-tool FastAPI routes (live under {api_prefix}/tools/<name>)
    for router in agent.get_tool_routers():
        app.include_router(router, prefix=config.api_prefix)
    if builtin_agent is not None:
        for router in builtin_agent.get_tool_routers():
            app.include_router(router, prefix=config.api_prefix)

    _mount_protocol_routes(app)

    # End-user chat at /. Ships in every runtime (unlike the dev UI) — it's the
    # public face of a deployed agent and talks to the live /invocations route.
    from ._ui_root_chat import build_root_chat_router

    app.include_router(build_root_chat_router())

    # Dev UI (optional). The app must still serve if this fails, so the failure
    # is swallowed — but recorded on app.state for local diagnosis, not hidden.
    app.state.dev_ui_mount_error = None
    if os.environ.get("APX_DEV_UI") != "0":
        try:
            from ._dev import build_dev_ui_router

            app.include_router(build_dev_ui_router(config.api_prefix))
            logger.info("Dev UI mounted at /_apx/*")
        except Exception as e:
            app.state.dev_ui_mount_error = str(e)
            logger.info("Dev UI not mounted (%s: %s)", type(e).__name__, e)

    # Auto-register with agent registry (if configured)
    if config.registry:
        public_url = _resolve_env_var(config.url) if config.url else ""
        registry_url = _resolve_env_var(config.registry)
        if registry_url:
            _schedule_registration(app, registry_url, public_url)
        else:
            logger.warning(
                "registry env var resolved to empty — skipping registration"
            )

    return ctx


def _schedule_registration(
    app: FastAPI, registry_url: str, public_url: str
) -> None:
    """Fire-and-forget POST to the agent registry after the server is up.

    Registry crawls ``{public_url}/.well-known/agent.json`` to populate the card.
    """
    import asyncio

    import httpx
    from starlette.middleware.base import BaseHTTPMiddleware

    async def _register() -> bool:
        await asyncio.sleep(2)

        url = registry_url.rstrip("/")
        payload: dict[str, Any] = {}
        if public_url:
            payload["url"] = public_url.rstrip("/")
        else:
            logger.warning(
                "No public URL configured (set url in [tool.apx.agent]) — "
                "registry may not be able to crawl this agent"
            )

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{url}/api/agents/register", json=payload
                )
                r.raise_for_status()
                data = r.json()
                logger.info(
                    "Registered with agent registry at %s as '%s'",
                    url,
                    data.get("id", "unknown"),
                )
                return True
        except Exception as e:
            logger.warning(
                "Failed to register with agent registry at %s: %s", url, e
            )
        return False

    class _RegisterOnceMiddleware(BaseHTTPMiddleware):
        _registered = False
        # Strong reference to the in-flight attempt so the event loop (which
        # keeps only a weak ref to bare tasks) can't GC it mid-flight (#378).
        _task: "asyncio.Task[Any] | None" = None

        async def dispatch(self, request: Any, call_next: Any) -> Any:
            cls = _RegisterOnceMiddleware
            # Launch one attempt when not yet registered and none in flight.
            # The check→set is synchronous (no await), so it can't double-launch.
            if not cls._registered and cls._task is None:
                cls._task = asyncio.create_task(cls._attempt())
            return await call_next(request)

        @staticmethod
        async def _attempt() -> None:
            try:
                # Only mark registered on SUCCESS, so a failed attempt retries on
                # the next request instead of being permanently skipped (#378).
                _RegisterOnceMiddleware._registered = await _register()
            finally:
                _RegisterOnceMiddleware._task = None

    app.add_middleware(_RegisterOnceMiddleware)


def _mount_protocol_routes(app: FastAPI) -> None:
    """Mount discovery + health + MCP routes."""
    protocol_router = APIRouter()

    @protocol_router.get("/.well-known/agent.json", include_in_schema=False)
    async def agent_card(request: Request) -> AgentCard:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            raise HTTPException(
                status_code=404, detail="Agent protocol not configured"
            )
        base = str(request.base_url).rstrip("/")
        mcp_available = (
            getattr(request.app.state, "mcp_server", None) is not None
        )
        return ctx.card.model_copy(
            update={
                "url": base,
                "mcpEndpoint": f"{base}/mcp" if mcp_available else None,
            }
        )

    @protocol_router.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    class _RawResponse(Response):
        """Sentinel for handlers that write directly to the ASGI socket."""

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            pass

    @protocol_router.get("/mcp/sse", include_in_schema=False)
    async def mcp_sse(request: Request) -> Response:
        """MCP SSE transport — connect MCP clients here."""
        mcp_server = getattr(request.app.state, "mcp_server", None)
        mcp_transport = getattr(request.app.state, "mcp_transport", None)
        if mcp_server is None or mcp_transport is None:
            raise HTTPException(
                status_code=503, detail="MCP server not available"
            )
        from ._mcp import set_mcp_auth

        set_mcp_auth(
            request.headers.get("Authorization", ""),
            request.headers.get("X-Forwarded-Access-Token", ""),
        )
        async with mcp_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options(),
            )
        return _RawResponse()

    @protocol_router.post("/mcp/messages/", include_in_schema=False)
    async def mcp_messages(request: Request) -> Response:
        mcp_transport = getattr(request.app.state, "mcp_transport", None)
        if mcp_transport is None:
            raise HTTPException(
                status_code=503, detail="MCP server not available"
            )
        await mcp_transport.handle_post_message(
            request.scope, request.receive, request._send
        )
        return _RawResponse()

    async def _mcp_http(request: Request) -> Response:
        """Stateless MCP HTTP transport — used by Genie Code and AI Playground."""
        mcp_http_manager = getattr(request.app.state, "mcp_http_manager", None)
        if mcp_http_manager is None:
            raise HTTPException(
                status_code=503, detail="MCP server not available"
            )
        from ._mcp import set_mcp_auth

        set_mcp_auth(
            request.headers.get("Authorization", ""),
            request.headers.get("X-Forwarded-Access-Token", ""),
        )
        scope = dict(request.scope)
        headers = list(scope.get("headers", []))
        accept_vals = [v for k, v in headers if k.lower() == b"accept"]
        has_json = any(b"application/json" in v for v in accept_vals)
        has_sse = any(b"text/event-stream" in v for v in accept_vals)
        if not has_json or not has_sse:
            headers = [(k, v) for k, v in headers if k.lower() != b"accept"]
            existing = b", ".join(accept_vals)
            required = []
            if not has_json:
                required.append(b"application/json")
            if not has_sse:
                required.append(b"text/event-stream")
            new_accept = b", ".join(required)
            if existing:
                new_accept = new_accept + b", " + existing
            headers.append((b"accept", new_accept))
            scope["headers"] = headers
        await mcp_http_manager.handle_request(
            scope, request.receive, request._send
        )
        return _RawResponse()

    protocol_router.add_api_route(
        "/mcp",
        endpoint=_mcp_http,
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )

    app.include_router(protocol_router)


async def _setup_mcp(app: FastAPI, ctx: AgentContext) -> Any:
    """Initialize MCP server + transports. Returns lifecycle context manager."""
    from contextlib import nullcontext

    # Tri-state: None when fine OR not-configured; str(exc) when intended-but-errored.
    app.state.mcp_mount_error = None
    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    except ImportError as _imp_exc:
        # The optional ``mcp`` extra isn't installed — expected, NOT an error.
        app.state.mcp_server = None
        app.state.mcp_transport = None
        app.state.mcp_http_manager = None
        app.state.mcp_mount_error = None
        logger.info(
            "MCP server not configured (%s: %s). pip install apx-agent[mcp] to enable.",
            type(_imp_exc).__name__, _imp_exc,
        )
        return nullcontext()

    try:
        _mcp = _build_mcp_components(ctx, app, ctx.config.api_prefix)
        app.state.mcp_server = _mcp.server
        app.state.mcp_transport = _mcp.sse_transport
        mcp_http_manager = StreamableHTTPSessionManager(_mcp.server, stateless=True)
        app.state.mcp_http_manager = mcp_http_manager
        logger.info(
            "MCP server enabled at /mcp/sse (SSE) and /mcp (stateless HTTP)"
        )
        return mcp_http_manager.run()
    except Exception as _mcp_exc:
        # The extra is present but setup genuinely failed — a real degradation.
        # No pip-install framing here: this is intended-but-errored, not absent.
        app.state.mcp_server = None
        app.state.mcp_transport = None
        app.state.mcp_http_manager = None
        app.state.mcp_mount_error = str(_mcp_exc)
        logger.warning(
            "MCP server failed to initialize (%s: %s) — /mcp will 503.",
            type(_mcp_exc).__name__, _mcp_exc,
        )
        return nullcontext()


def create_app(
    agent: "BaseAgent | None" = None,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
    conversation_store: Any | None = None,
) -> FastAPI:
    """Create a complete FastAPI app: ``/invocations`` + discovery + MCP + dev UI.

    ``pyproject_path`` can be an explicit path to ``pyproject.toml``. When
    omitted, the config is discovered from the entry-point module's location
    or the current working directory.

    ``conversation_store`` is an optional ``ConversationStore`` (e.g. ``LakebaseConversationStore``)
    for multi-turn memory. When provided, conversation history is persisted
    across requests keyed by ``conversation_id``.

    Example::

        from apx_agent import LlmAgent, Dependencies, create_app

        def get_billing(customer_id: str, ws: Dependencies.Workspace) -> dict:
            \"\"\"Get billing history.\"\"\"
            ...

        agent = LlmAgent(tools=[get_billing])
        app = create_app(agent)
        # uvicorn my_app:app --reload
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # MLflow auto-tracing. Enabled by default so hand-authored create_app()
        # services get the same LangChain/LangGraph spans as scaffolded Apps.
        # Set APX_AGENT_MLFLOW_AUTOLOG=0 to force the cheaper selective-span path.
        try:
            from ._mlflow_tracing import autolog_if_env

            autolog_if_env()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("MLflow autolog setup skipped: %s", exc)

        # Install the in-process trace-capture SpanProcessor so the dev-UI Trace
        # detail can serve recent runs from memory (FEVM/private-link blocks the
        # blob egress mlflow.get_trace falls through to). Best-effort.
        try:
            from ._trace_store import install_capture_processor_at_startup

            install_capture_processor_at_startup()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("Trace-capture processor install skipped: %s", exc)

        # Best-effort: a freshly scaffolded agent run locally with `apx-agent run`
        # has no Databricks credentials configured yet. Don't let that crash
        # startup — boot the server (so the dev UI loads) and surface a clear
        # error only when a tool actually needs the client.
        try:
            app.state.workspace_client = _make_workspace_client()
        except Exception as exc:
            logger.warning(
                "No Databricks credentials resolved — workspace client unavailable. "
                "The server will start, but tool calls that hit Databricks will fail "
                "until you configure auth (https://docs.databricks.com/dev-tools/auth). "
                "Cause: %s",
                exc,
            )
            app.state.workspace_client = None
        app.state.conversation_store = conversation_store

        ctx = await setup_agent(
            app, agent, config, pyproject_path=pyproject_path
        )

        # /readyz — the capability self-test the deploy gate and `agents
        # status` probe (#449). The Apps template mounts it in start_server.py;
        # plain create_app (all local runs) must serve it too, or every local
        # probe 404s. mount_readyz is idempotent, so a caller who already
        # mounted it isn't double-registered.
        if ctx is not None:
            try:
                from ._readyz import mount_readyz

                mount_readyz(app, ctx.agent, model=ctx.config.model)
            except Exception as exc:
                logger.warning("Skipping /readyz mount: %s", exc)

        # Mount the supported /invocations + /responses routes.
        # Both use the same resolved conversation_store. Best-effort — missing
        # optional deps log a warning and skip the affected route only.
        if ctx is not None:
            _store: Any = None
            _checkpointer: Any = None
            try:
                from ._invocations import mount_invocations_route, mount_responses_route
                from ._memory_wiring import (  # noqa: PLC0415
                    _lakebase_checkpointer_target,
                    resolve_checkpointer,
                    resolve_conversation_store,
                )

                _store = resolve_conversation_store(
                    ctx.config,
                    ws=app.state.workspace_client,
                    override=conversation_store,
                    agent=ctx.agent,
                )
                # Held for shutdown: dispose the Lakebase engines we built here
                # (#376). Only when WE built the store — an explicit override is
                # caller-owned. The declared memory/example stores (built in
                # finalize_agent) are reachable via ctx.agent._declared_stores.
                if conversation_store is None:
                    app.state.disposable_stores = [_store, *getattr(ctx.agent, "_declared_stores", [])]
                else:
                    app.state.disposable_stores = list(getattr(ctx.agent, "_declared_stores", []))
                # Durable checkpointer (Lakebase → PostgresSaver) so a pending
                # mid-turn approval survives a restart. None → in-process default.
                # An explicit conversation_store override means the caller owns
                # session state, so no auto-checkpointer is built (store_override).
                _checkpointer = resolve_checkpointer(
                    ctx.config, ws=app.state.workspace_client, agent=ctx.agent,
                    store_override=conversation_store,
                )
                # Held for shutdown: a durable checkpointer owns a Lakebase pool
                # that must be closed on teardown (#346).
                app.state.checkpointer = _checkpointer
                # #490: a durable checkpointer was expected (a lakebase session on
                # an LlmAgent) but the build failed → mark degraded so /readyz
                # reports it instead of silently running in-process memory (where
                # approvals stop surviving restarts). No lakebase / composite
                # agent / store-override → target is None → not degraded.
                _cp_expected = _lakebase_checkpointer_target(
                    ctx.config, app.state.workspace_client, ctx.agent, conversation_store
                ) is not None
                app.state.checkpointer_degraded = _cp_expected and _checkpointer is None
                mount_invocations_route(
                    app, ctx.agent, ctx.config,
                    conversation_store=_store, checkpointer=_checkpointer,
                )
            except Exception as exc:
                logger.warning("Skipping /invocations mount: %s", exc)

            try:
                from ._a2a import mount_a2a_route

                mount_a2a_route(
                    app, ctx.agent, ctx.config,
                    conversation_store=_store, checkpointer=_checkpointer,
                )
            except Exception as exc:
                logger.warning("Skipping A2A mount: %s", exc)

            try:
                from ._invocations import mount_responses_route

                mount_responses_route(
                    app, ctx.agent, ctx.config,
                    conversation_store=_store, checkpointer=_checkpointer,
                )
            except Exception as exc:
                logger.warning("Skipping /responses mount: %s", exc)

        if ctx is not None:
            mcp_lifecycle = await _setup_mcp(app, ctx)
        else:
            from contextlib import nullcontext

            mcp_lifecycle = nullcontext()

        async with mcp_lifecycle:
            try:
                yield
            finally:
                logger.info("Shutting down agent runtime")
                from ._memory_wiring import close_checkpointer, dispose_store_engine  # noqa: PLC0415

                close_checkpointer(getattr(app.state, "checkpointer", None))
                for _s in getattr(app.state, "disposable_stores", []):
                    dispose_store_engine(_s)
                ws = getattr(app.state, "workspace_client", None)
                if ws and hasattr(ws, "close"):
                    try:
                        ws.close()
                    except Exception:
                        pass

    return FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Mounting helpers — for embedding apx-agent's protocol surface (MCP + A2A
# discovery + health) onto an existing FastAPI app (e.g. one produced by
# ``mlflow.genai.agent_server.AgentServer`` in the Databricks Apps target).
# ---------------------------------------------------------------------------


def mount_mcp_endpoints(
    app: FastAPI,
    agent: BaseAgent,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
) -> None:
    """Mount apx-agent's ``/mcp`` + ``/.well-known/agent.json`` + ``/health``
    on an existing FastAPI app.

    Designed for the Databricks Apps target: pair the
    ``mlflow.genai.agent_server.AgentServer`` (which provides ``/invocations``
    + ``/responses``) with apx-agent's MCP surface so Genie / Genie Code can
    consume the same agent as an MCP source.

    Mounted routes are inert until the FastAPI lifespan completes startup
    (they ``503`` if ``app.state.mcp_server`` isn't populated yet). The
    lifespan registers an ``async def startup_event_handler`` that runs
    ``setup_agent`` + ``_setup_mcp`` and stores the resulting state on
    ``app.state``. Shutdown closes the MCP HTTP manager cleanly.

    Usage::

        from mlflow.genai.agent_server import AgentServer
        from apx_agent import mount_mcp_endpoints

        server = AgentServer(agent_type="ResponsesAgent")
        from agent_server import agent  # your apx-agent BaseAgent

        mount_mcp_endpoints(server.app, agent.agent)
        app = server.app

    The mount is best-effort: when the ``mcp`` extra is missing, the routes
    are still registered but return 503 — same behavior as ``create_app``.

    Requires installing ``apx-agent[mcp]`` for full functionality.
    """
    # Mount the route shells immediately. They read from app.state — which
    # gets populated in the lifespan startup event below.
    _mount_protocol_routes(app)

    # End-user chat at / — ships in every runtime, including Apps.
    from ._ui_root_chat import build_root_chat_router

    app.include_router(build_root_chat_router())

    # Dev UI (/_apx/*) — Chat, Discover, Edit, Probe, Topology, Eval.
    # Mounted in local ``apx run`` AND deployed Databricks Apps so peers can
    # discover each other from a live App URL. Ordinary write routes stay
    # gated by Apps SSO (or optional ``APX_DEV_UI_TOKEN`` for automation).
    # Discover wire/unwire additionally requires ``APX_DEV_UI_TOKEN`` on Apps
    # (#611 — shared live-agent mutation). See ``_dev._enforce_dev_write_auth``.
    app.state.dev_ui_mount_error = None
    if os.environ.get("APX_DEV_UI") != "0":
        try:
            from ._dev import build_dev_ui_router

            app.include_router(build_dev_ui_router())
            logger.info("Dev UI mounted at /_apx/*")
        except Exception as exc:  # pragma: no cover — optional dep
            app.state.dev_ui_mount_error = str(exc)
            logger.info("Dev UI not mounted (%s: %s)", type(exc).__name__, exc)

    # Track the in-flight MCP lifecycle so shutdown can close it cleanly.
    _state_key = "_apx_mount_state"

    @app.on_event("startup")
    async def _apx_mount_startup() -> None:  # type: ignore[misc]
        # MLflow auto-tracing. This is the apps-target path (the AgentServer app
        # is not created via create_app, so create_app's lifespan never runs
        # here). Under ``apx-agent run --reload`` the worker subprocess re-imports the
        # module and re-runs this startup, but never re-runs cli run()'s body —
        # so autolog must be (re)applied in-process here or per-tool/per-LLM
        # spans stop emitting under --reload (audit M5).
        try:
            from ._mlflow_tracing import autolog_if_env

            autolog_if_env()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("MLflow autolog setup skipped: %s", exc)

        # Install the in-process trace-capture SpanProcessor so the dev-UI Trace
        # detail can serve recent runs from memory (FEVM/private-link blocks the
        # blob egress mlflow.get_trace falls through to). Best-effort.
        try:
            from ._trace_store import install_capture_processor_at_startup

            install_capture_processor_at_startup()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("Trace-capture processor install skipped: %s", exc)

        ctx = await setup_agent(app, agent, config, pyproject_path=pyproject_path)
        if ctx is None:
            logger.info("mount_mcp_endpoints: no agent config — /mcp will 503")
            return
        try:
            mcp_lifecycle = await _setup_mcp(app, ctx)
        except Exception as exc:  # pragma: no cover — defensive
            # _setup_mcp swallows its own errors; this only fires on something
            # truly unexpected. Record it as a real degradation.
            app.state.mcp_mount_error = str(exc)
            logger.warning(
                "mount_mcp_endpoints: MCP setup failed (%s) — /mcp will 503",
                exc,
            )
            return
        cm = mcp_lifecycle.__aenter__()
        try:
            await cm
        except Exception as exc:  # pragma: no cover
            app.state.mcp_mount_error = str(exc)
            logger.warning("mount_mcp_endpoints: failed to enter MCP lifecycle: %s", exc)
            setattr(app.state, _state_key, None)
            return
        setattr(app.state, _state_key, mcp_lifecycle)
        logger.info("mount_mcp_endpoints: /mcp ready (HTTP + SSE)")

    @app.on_event("shutdown")
    async def _apx_mount_shutdown() -> None:  # type: ignore[misc]
        lifecycle = getattr(app.state, _state_key, None)
        if lifecycle is None:
            return
        try:
            await lifecycle.__aexit__(None, None, None)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("mount_mcp_endpoints: clean shutdown failed: %s", exc)
