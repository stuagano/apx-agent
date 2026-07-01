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

from ._agents import BaseAgent
from ._prompt_assembly import compose_instructions
from ._defaults import _make_workspace_client
from ._inspection import _load_agent_config
from ._mcp import _build_mcp_components
from ._models import (
    A2ASkill,
    AgentCard,
    AgentConfig,
    AgentContext,
)

logger = logging.getLogger(__name__)

# Env var references that fail to resolve (var not set) expand to empty string.
# Callers are expected to check `if not resolved:` and skip/warn accordingly.
_UNSET_ENV_EXPANSION = ""


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


def apply_config_guardrails(agent: BaseAgent, config: AgentConfig) -> None:
    """Apply ``[tool.apx.agent.guardrails]`` config onto the live agent instance.

    Translates ``config.guardrails`` (a ``GuardrailsConfig``) into built-in
    guard callables and attaches them additively:

    - ``before_tool`` gates (deny / allow / rate-limit) are merged via
      ``compose(existing_code_hook, *config_gates)`` — code hook runs first.
    - ``input_guardrails`` (injection heuristic) are appended — code guards
      run first.

    Idempotent via the ``_apx_config_guards_applied`` sentinel: a second call
    is a no-op.  ``setup_agent`` can run more than once on the same instance
    (``mount_mcp_endpoints`` fires its own ``setup_agent`` at startup), so this
    is a real correctness requirement, not a nicety.

    Warns (never crashes) when guards are declared on a composition root
    (e.g. ``SequentialAgent``) that has no ``_before_tool`` /
    ``_input_guardrails`` — matches the ``sub_agents``-merge precedent.
    """
    if getattr(agent, "_apx_config_guards_applied", False):
        return

    from ._guards import build_config_guards, compose  # noqa: PLC0415

    _guards = build_config_guards(config.guardrails)

    if _guards.input_guardrails:
        existing_igs = getattr(agent, "_input_guardrails", None)
        if existing_igs is None:
            logger.warning(
                "config guardrails.injection_detection set on a %s root, "
                "which has no _input_guardrails (only LlmAgent does) — ignored.",
                type(agent).__name__,
            )
        else:
            existing_igs.extend(_guards.input_guardrails)

    if _guards.before_tool is not None:
        if not hasattr(agent, "_before_tool"):
            logger.warning(
                "config guardrails tool rules (blocked_tools / allowed_tools / "
                "rate_limit) set on a %s root, which has no _before_tool "
                "(only LlmAgent does) — ignored.",
                type(agent).__name__,
            )
        else:
            code_hook = getattr(agent, "_before_tool", None)
            if code_hook is not None:
                setattr(agent, "_before_tool", compose(code_hook, _guards.before_tool))
            else:
                setattr(agent, "_before_tool", _guards.before_tool)

    setattr(agent, "_apx_config_guards_applied", True)


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


def _resolve_env_var(value: str) -> str:
    """Resolve a ``$VAR`` or ``${VAR}`` reference to its environment value.

    Returns the original string unchanged if it doesn't start with ``$``
    or the variable is not set.
    """
    if not value.startswith("$"):
        return value
    var_name = value.lstrip("$").strip("{}")
    return os.environ.get(var_name, _UNSET_ENV_EXPANSION)


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

    tools = agent.collect_tools()
    tools += await agent.fetch_remote_tools()
    card = AgentCard(
        name=config.name,
        description=config.description,
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

    # Per-tool FastAPI routes (live under {api_prefix}/tools/<name>)
    for router in agent.get_tool_routers():
        app.include_router(router, prefix=config.api_prefix)

    _mount_protocol_routes(app)

    # End-user chat at /. Ships in every runtime (unlike the dev UI) — it's the
    # public face of a deployed agent and talks to the live /invocations route.
    from ._ui_root_chat import build_root_chat_router

    app.include_router(build_root_chat_router())

    # Dev UI (optional). The app must still serve if this fails, so the failure
    # is swallowed — but recorded on app.state for local diagnosis, not hidden.
    app.state.dev_ui_mount_error = None
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

    async def _register() -> None:
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
        except Exception as e:
            logger.warning(
                "Failed to register with agent registry at %s: %s", url, e
            )

    class _RegisterOnceMiddleware(BaseHTTPMiddleware):
        _registered = False

        async def dispatch(self, request: Any, call_next: Any) -> Any:
            if not _RegisterOnceMiddleware._registered:
                _RegisterOnceMiddleware._registered = True
                asyncio.create_task(_register())
            return await call_next(request)

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

    ``conversation_store`` is an optional ``ConversationStore`` (e.g. ``DeltaConversationStore``)
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
        # MLflow auto-tracing. ``apx-agent run`` sets ``APX_AGENT_MLFLOW_AUTOLOG=1``
        # before importing the user's module so the dev loop gets per-tool +
        # per-LLM spans by default. Deploy paths reach this lifespan with the
        # env unset, so autolog stays off there (selective spans in the
        # compile path are always on either way).
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

        # Mount the supported /invocations + /responses routes.
        # Both use the same resolved conversation_store. Best-effort — missing
        # optional deps log a warning and skip the affected route only.
        if ctx is not None:
            _store: Any = None
            _checkpointer: Any = None
            try:
                from ._invocations import mount_invocations_route, mount_responses_route
                from ._memory_wiring import (  # noqa: PLC0415
                    resolve_checkpointer,
                    resolve_conversation_store,
                )

                _store = resolve_conversation_store(
                    ctx.config,
                    ws=app.state.workspace_client,
                    override=conversation_store,
                    agent=ctx.agent,
                )
                # Durable checkpointer (Lakebase → PostgresSaver) so a pending
                # mid-turn approval survives a restart. None → in-process default.
                # An explicit conversation_store override means the caller owns
                # session state, so no auto-checkpointer is built (store_override).
                _checkpointer = resolve_checkpointer(
                    ctx.config, ws=app.state.workspace_client, agent=ctx.agent,
                    store_override=conversation_store,
                )
                mount_invocations_route(
                    app, ctx.agent, ctx.config,
                    conversation_store=_store, checkpointer=_checkpointer,
                )
            except Exception as exc:
                logger.warning("Skipping /invocations mount: %s", exc)

            try:
                from ._a2a import mount_a2a_route

                mount_a2a_route(
                    app, ctx.agent, ctx.config, conversation_store=_store
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

    # End-user chat at / — ships in every runtime, including Apps (unlike the
    # dev UI below). Mounted before the DATABRICKS_APP_PORT gate on purpose.
    from ._ui_root_chat import build_root_chat_router

    app.include_router(build_root_chat_router())

    # Dev UI (/_apx/*) — available when running locally with `apx-agent run`.
    # Absent in production Apps deployments (DATABRICKS_APP_PORT is set by
    # the Apps runtime). The mount is call-time so it runs before the startup
    # event and the routes are registered on the first request.
    if not os.environ.get("DATABRICKS_APP_PORT"):
        # Swallowed so the app still serves; recorded on app.state for local
        # diagnosis. No /readyz check — dev-UI is intentionally off in deploys.
        app.state.dev_ui_mount_error = None
        try:
            from ._dev import build_dev_ui_router

            app.include_router(build_dev_ui_router())
            logger.info("Dev UI mounted at /_apx/* (local dev mode)")
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
