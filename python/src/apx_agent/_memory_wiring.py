"""E3b: Declarative memory/session backend attach logic.

Two entry points:

- ``attach_declared_memory(agent, config, ws)`` — build config-declared
  memory + example stores and merge them into the agent via
  ``agent._register_tool``.  Called from ``finalize_agent`` before the
  A2A card snapshot (``_wiring.py:229``).  Idempotent via sentinel.

- ``resolve_session_store(config, ws, override=None)`` — return the
  explicit override if provided; else build a session store from
  ``config.session``; else ``None``.  Called in the ``create_app``
  lifespan to feed ``mount_invocations_route``.

Principal threading: memory tools emitted here carry a
``_principal: Dependencies.Principal`` dep param (via
``make_memory_tools(_use_dep_principal=True)``) so the per-request OBO
identity threads through the resolved-deps closure — the same mechanism
that delivers ``ctx.ws`` (proven in Phase 0, Task 0.2).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._models import AgentConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Store factories
# ---------------------------------------------------------------------------


def _build_memory_store(cfg: Any, ws: Any | None) -> Any:
    """Build the memory store for the given config.

    Raises ``ValueError`` for missing required fields.
    Returns ``None`` when lakebase/delta is requested with ``ws=None``
    (caller logs and skips).
    """
    from ._memory import InMemoryMemoryStore  # noqa: PLC0415

    if cfg.type == "inmemory":
        return InMemoryMemoryStore()

    if cfg.type == "delta":
        if ws is None:
            return None
        if not cfg.table_name:
            raise ValueError(
                "[tool.apx.agent.memory] type='delta' requires table_name "
                "(e.g. 'catalog.schema.apx_memories')."
            )
        from ._memory_delta import DeltaMemoryStore  # noqa: PLC0415
        from ._embeddings import make_embedding_fn  # noqa: PLC0415
        from ._sql import run_sql  # noqa: PLC0415

        embed_fn = None
        if cfg.embedding_model:
            if cfg.embedding_dim is None:
                raise ValueError(
                    "[tool.apx.agent.memory] type='delta' with embedding_model "
                    "requires embedding_dim."
                )
            embed_fn = make_embedding_fn(ws, cfg.embedding_model)

        return DeltaMemoryStore(
            run_sql=lambda q: run_sql(ws, q),
            embedding_fn=embed_fn,
            embedding_dim=cfg.embedding_dim,
            table_name=cfg.table_name,
            auto_create=cfg.auto_create,
            index_name=cfg.index_name,
        )

    if cfg.type == "lakebase":
        if ws is None:
            return None
        if not cfg.instance_name:
            raise ValueError(
                "[tool.apx.agent.memory] type='lakebase' requires instance_name."
            )
        if not cfg.database:
            raise ValueError(
                "[tool.apx.agent.memory] type='lakebase' requires database."
            )
        if not cfg.embedding_model:
            raise ValueError(
                "[tool.apx.agent.memory] type='lakebase' requires embedding_model."
            )
        if cfg.embedding_dim is None:
            raise ValueError(
                "[tool.apx.agent.memory] type='lakebase' requires embedding_dim."
            )
        from ._lakebase_engine import build_lakebase_engine  # noqa: PLC0415
        from ._embeddings import make_embedding_fn  # noqa: PLC0415
        from ._memory_lakebase import LakebaseMemoryStore  # noqa: PLC0415

        # Resolve $ENV_VAR in host — imported locally to avoid top-level
        # _memory_wiring → _wiring cycle (Task 1.5 makes _wiring import us).
        from ._wiring import _resolve_env_var  # noqa: PLC0415

        host = _resolve_env_var(cfg.host) if cfg.host else None

        engine = build_lakebase_engine(
            ws=ws,
            instance_name=cfg.instance_name,
            database=cfg.database,
            host=host,
        )
        embed_fn = make_embedding_fn(ws, cfg.embedding_model)
        return LakebaseMemoryStore(
            engine=engine,
            embedding_fn=embed_fn,
            embedding_dim=cfg.embedding_dim,
            table_name=cfg.table_name or "apx_memories",
            auto_create=cfg.auto_create,
            ensure_extension=cfg.ensure_extension,
        )

    raise ValueError(
        f"[tool.apx.agent.memory] unknown type {cfg.type!r}. "
        "Known: inmemory, delta, lakebase."
    )


def _build_session_store(cfg: Any, ws: Any | None) -> Any | None:
    """Build a SessionStore from SessionBackendConfig.  Returns None on failure."""
    if cfg.type == "inmemory":
        from ._session import InMemorySessionStore  # noqa: PLC0415

        return InMemorySessionStore()

    if cfg.type == "delta":
        if ws is None:
            logger.warning(
                "[tool.apx.agent.session] type='delta' requires ws; "
                "skipping session store (deploy with valid Databricks credentials)."
            )
            return None
        if not cfg.table_name:
            raise ValueError(
                "[tool.apx.agent.session] type='delta' requires table_name "
                "(three-part UC name: catalog.schema.table)."
            )
        from ._session_delta import DeltaSessionStore  # noqa: PLC0415

        # DeltaSessionStore takes `table_path` (not `table_name`); verified
        # against _session_delta.py:72-88.
        return DeltaSessionStore(
            ws=ws,
            table_path=cfg.table_name,  # table_name field → table_path param
            warehouse_id=getattr(cfg, "warehouse_id", None),
            auto_create=cfg.auto_create,
        )

    if cfg.type == "lakebase":
        if ws is None:
            logger.warning(
                "[tool.apx.agent.session] type='lakebase' requires ws; "
                "skipping session store (deploy with valid Databricks credentials)."
            )
            return None
        if not cfg.instance_name or not cfg.database:
            raise ValueError(
                "[tool.apx.agent.session] type='lakebase' requires instance_name and database."
            )
        from ._lakebase_engine import build_lakebase_engine  # noqa: PLC0415
        from ._session_lakebase import LakebaseSessionStore  # noqa: PLC0415
        from ._wiring import _resolve_env_var  # noqa: PLC0415

        host = _resolve_env_var(cfg.host) if cfg.host else None
        engine = build_lakebase_engine(
            ws=ws, instance_name=cfg.instance_name, database=cfg.database, host=host
        )
        return LakebaseSessionStore(
            engine=engine,
            table_name=cfg.table_name or "apx_sessions",
            auto_create=cfg.auto_create,
        )

    raise ValueError(
        f"[tool.apx.agent.session] unknown type {cfg.type!r}. "
        "Known: inmemory, delta, lakebase."
    )


def _build_example_store(cfg: Any, ws: Any | None) -> Any | None:
    """Build an ExampleStore from ExampleBackendConfig.

    Example stores isolate by ``agent_id`` (not ``principal_id``) — examples
    are coworker-scoped, not per-user.  Uses ``InMemoryExampleStore``,
    ``DeltaExampleStore``, or ``LakebaseExampleStore`` — distinct classes from
    the MemoryStore hierarchy (different schema, different tool names).
    """
    if cfg.type == "inmemory":
        from ._example import InMemoryExampleStore  # noqa: PLC0415

        return InMemoryExampleStore()

    if cfg.type == "delta":
        if ws is None:
            return None
        if not cfg.table_name:
            raise ValueError(
                "[tool.apx.agent.example] type='delta' requires table_name."
            )
        from ._example_delta import DeltaExampleStore  # noqa: PLC0415
        from ._embeddings import make_embedding_fn  # noqa: PLC0415
        from ._sql import run_sql  # noqa: PLC0415

        embed_fn = None
        if cfg.embedding_model:
            if cfg.embedding_dim is None:
                raise ValueError(
                    "[tool.apx.agent.example] type='delta' with embedding_model "
                    "requires embedding_dim."
                )
            embed_fn = make_embedding_fn(ws, cfg.embedding_model)

        # DeltaExampleStore.__init__(run_sql, embedding_fn, embedding_dim, table_name,
        # auto_create) — verified against _example_delta.py:103-142.
        return DeltaExampleStore(
            run_sql=lambda q: run_sql(ws, q),
            embedding_fn=embed_fn,
            embedding_dim=cfg.embedding_dim,
            table_name=cfg.table_name,
            auto_create=cfg.auto_create,
        )

    if cfg.type == "lakebase":
        if ws is None:
            return None
        if not cfg.instance_name or not cfg.database:
            raise ValueError(
                "[tool.apx.agent.example] type='lakebase' requires instance_name and database."
            )
        if not cfg.embedding_model or cfg.embedding_dim is None:
            raise ValueError(
                "[tool.apx.agent.example] type='lakebase' requires embedding_model and embedding_dim."
            )
        from ._lakebase_engine import build_lakebase_engine  # noqa: PLC0415
        from ._embeddings import make_embedding_fn  # noqa: PLC0415
        from ._example_lakebase import LakebaseExampleStore  # noqa: PLC0415
        from ._wiring import _resolve_env_var  # noqa: PLC0415

        host = _resolve_env_var(cfg.host) if cfg.host else None
        engine = build_lakebase_engine(
            ws=ws, instance_name=cfg.instance_name, database=cfg.database, host=host
        )
        embed_fn = make_embedding_fn(ws, cfg.embedding_model)
        # LakebaseExampleStore.__init__(engine, embedding_fn, embedding_dim, table_name,
        # auto_create, ensure_extension) — verified against _example_lakebase.py:154-177.
        return LakebaseExampleStore(
            engine=engine,
            embedding_fn=embed_fn,
            embedding_dim=cfg.embedding_dim,
            table_name=cfg.table_name or "apx_examples",
            auto_create=cfg.auto_create,
            ensure_extension=cfg.ensure_extension,
        )

    raise ValueError(
        f"[tool.apx.agent.example] unknown type {cfg.type!r}. "
        "Known: inmemory, delta, lakebase."
    )


# ---------------------------------------------------------------------------
# Attach helpers
# ---------------------------------------------------------------------------


def _resolve_default_principal(ws: Any | None) -> str | None:
    """Local-dev fallback principal, from the Databricks CLI profile identity.

    A deployed Databricks App gets the per-request OBO principal from the
    ``X-Forwarded-User`` header, so this returns ``None`` there (the per-request
    principal wins). Local ``apx-agent run`` has no OBO header — without a principal
    the memory tools no-op ("No principal_id available"). Resolve the CLI
    profile's current user (``ws.current_user.me()``) as the default so memory
    works in the dev loop. Best-effort; never raises.
    """
    from ._obo import _in_databricks_app  # noqa: PLC0415 (avoid import cycle)

    if ws is None or _in_databricks_app():
        return None
    try:
        return ws.current_user.me().user_name
    except Exception:
        return None


def attach_declared_memory(
    agent: Any,
    config: "AgentConfig",
    ws: Any | None,
) -> None:
    """Build config-declared memory/example tools and register them on the agent.

    Called from ``finalize_agent`` BEFORE ``agent.collect_tools()`` so the
    minted tools appear in both the A2A card and the compiled LangGraph.

    Idempotent via ``_apx_memory_attached`` sentinel.  Code-wired tools win
    on name collision (same rule as ``merge_config_tools``).
    """
    if getattr(agent, "_apx_memory_attached", False):
        return

    register = getattr(agent, "_register_tool", None)
    if register is None:
        logger.warning(
            "[tool.apx.agent.memory/example] declared on a %s root that has no "
            "_register_tool — skipping (attach on a leaf LlmAgent).",
            type(agent).__name__,
        )
        setattr(agent, "_apx_memory_attached", True)
        return

    existing = {getattr(fn, "__name__", None) for fn in getattr(agent, "_tool_fns", [])}

    from ._memory_tools import make_memory_tools  # noqa: PLC0415
    from ._example_tools import make_example_tools  # noqa: PLC0415

    # --- memory ---
    # Explicit [tool.apx.agent.memory] block wins; else use the agent-carried
    # config (e.g. CoworkerAgent.memory_config). The framework supplies ``ws``.
    mcfg = config.memory if config.memory is not None else getattr(agent, "memory_config", None)
    if mcfg is not None:
        store = None
        try:
            store = _build_memory_store(mcfg, ws)
        except (ValueError, ImportError) as exc:
            logger.warning(
                "[tool.apx.agent.memory] build failed — skipping memory tools: %s",
                exc,
            )
        if store is None and mcfg.type in ("lakebase", "delta"):
            if ws is None:
                logger.warning(
                    "[tool.apx.agent.memory] type=%r requires ws; ws=None at this point "
                    "(deploy with valid Databricks credentials). Memory tools will be absent.",
                    mcfg.type,
                )
                msg = f"{mcfg.type} memory needs a workspace/warehouse — not active"
            else:
                msg = f"{mcfg.type} memory build failed — check logs for details"
            setattr(agent, "_apx_memory_degraded", msg)
        if store is not None:
            default_principal = _resolve_default_principal(ws)
            # Expose store + scoping info for the dev UI /_apx/memories endpoint.
            setattr(agent, "_apx_memory_store", store)
            setattr(agent, "_apx_memory_principal", default_principal)
            setattr(agent, "_apx_memory_namespace", mcfg.namespace_default)
            # Config path uses the dep-principal mechanism (proved in Phase 0 Task 0.2).
            # _use_dep_principal=True emits tools with a `principal: Dependencies.Principal`
            # dep param (added in Task 1.3, which runs before this task).
            tools = make_memory_tools(
                store=store,
                _use_dep_principal=True,
                default_principal_id=default_principal,
                namespace_default=mcfg.namespace_default,
                tool_prefix=mcfg.tool_prefix,
                include=mcfg.include,  # type: ignore[arg-type]
            )
            for fn in tools:
                nm = getattr(fn, "__name__", None)
                if nm in existing:
                    logger.warning(
                        "[tool.apx.agent.memory] declares %r but the agent already wires "
                        "a tool with that name — keeping the code-wired tool, ignoring "
                        "the declared one. Use tool_prefix to mount both.",
                        nm,
                    )
                    continue
                register(fn)
                if nm:
                    existing.add(nm)

    # --- example ---
    if config.example is not None:
        ecfg = config.example
        estore = None
        try:
            estore = _build_example_store(ecfg, ws)
        except (ValueError, ImportError) as exc:
            logger.warning(
                "[tool.apx.agent.example] build failed — skipping example tools: %s",
                exc,
            )
        if estore is not None:
            agent_id = ecfg.agent_id or config.name
            example_tools = make_example_tools(
                store=estore,
                agent_id_resolver=lambda aid=agent_id: aid,
                tool_prefix=ecfg.tool_prefix,
                include=ecfg.include,  # type: ignore[arg-type]
            )
            for fn in example_tools:
                nm = getattr(fn, "__name__", None)
                if nm in existing:
                    logger.warning(
                        "[tool.apx.agent.example] declares %r but name collision — "
                        "keeping code-wired tool.",
                        nm,
                    )
                    continue
                register(fn)
                if nm:
                    existing.add(nm)

    setattr(agent, "_apx_memory_attached", True)


def resolve_session_store(
    config: "AgentConfig | None",
    ws: Any | None,
    override: Any | None = None,
    agent: Any | None = None,
) -> Any | None:
    """Return a SessionStore for this agent, or None.

    Precedence: explicit ``override`` arg > config ``session`` block >
    agent-carried ``session_config`` (e.g. CoworkerAgent) > None.
    """
    if override is not None:
        return override
    config_session = config.session if config is not None else None
    scfg = config_session if config_session is not None else getattr(agent, "session_config", None)
    if scfg is None:
        return None
    try:
        return _build_session_store(scfg, ws)
    except (ValueError, ImportError) as exc:
        logger.warning(
            "[tool.apx.agent.session] build failed — no session store: %s", exc
        )
        return None


def _build_conversation_store(cfg: Any, ws: Any | None) -> Any | None:
    """Build a ConversationStore from SessionBackendConfig. Returns None on failure.

    :param cfg: A ``SessionBackendConfig`` object with ``type``, ``table_name``,
        ``instance_name``, ``database``, ``host``, ``warehouse_id``, and
        ``auto_create`` fields.
    :param ws: ``WorkspaceClient`` required for Delta and Lakebase backends.
        ``None`` causes Delta/Lakebase to return ``None`` with a warning.
    :returns: A :class:`ConversationStore` instance, or ``None`` when the
        backend is unavailable.
    :raises ValueError: If required config fields are missing or the type is unknown.
    """
    if cfg.type == "inmemory":
        from ._conversation import InMemoryConversationStore  # noqa: PLC0415

        return InMemoryConversationStore()

    if cfg.type == "delta":
        if ws is None:
            logger.warning(
                "[tool.apx.agent.session] type='delta' requires ws; "
                "skipping conversation store (deploy with valid Databricks credentials)."
            )
            return None
        if not cfg.table_name:
            raise ValueError(
                "[tool.apx.agent.session] type='delta' requires table_name "
                "(three-part UC name: catalog.schema.base_prefix)."
            )
        from ._conversation_delta import DeltaConversationStore  # noqa: PLC0415

        return DeltaConversationStore(
            table_prefix=cfg.table_name,
            ws=ws,
            warehouse_id=getattr(cfg, "warehouse_id", None),
            auto_create=cfg.auto_create,
        )

    if cfg.type == "lakebase":
        if ws is None:
            logger.warning(
                "[tool.apx.agent.session] type='lakebase' requires ws; "
                "skipping conversation store (deploy with valid Databricks credentials)."
            )
            return None
        if not cfg.instance_name or not cfg.database:
            raise ValueError(
                "[tool.apx.agent.session] type='lakebase' requires instance_name and database."
            )
        from ._lakebase_engine import build_lakebase_engine  # noqa: PLC0415
        from ._conversation_lakebase import LakebaseConversationStore  # noqa: PLC0415
        from ._wiring import _resolve_env_var  # noqa: PLC0415

        host = _resolve_env_var(cfg.host) if cfg.host else None
        engine = build_lakebase_engine(
            ws=ws, instance_name=cfg.instance_name, database=cfg.database, host=host
        )
        base = cfg.table_name or "apx"
        return LakebaseConversationStore(
            engine=engine,
            conversations_table=f"{base}_conversations",
            items_table=f"{base}_conversation_items",
            auto_create=cfg.auto_create,
        )

    raise ValueError(
        f"[tool.apx.agent.session] unknown type {cfg.type!r}. "
        "Known: inmemory, delta, lakebase."
    )


def resolve_conversation_store(
    config: "AgentConfig | None",
    ws: Any | None,
    override: Any | None = None,
    agent: Any | None = None,
) -> Any | None:
    """Return a ConversationStore for this agent, or None.

    Precedence: explicit ``override`` arg > config ``session`` block >
    agent-carried ``session_config`` (e.g. CoworkerAgent) > None.

    :param config: The agent config; its ``session`` block drives backend selection.
    :param ws: ``WorkspaceClient`` for Delta/Lakebase backends. ``None`` downgrades
        those backends to ``None`` with a warning.
    :param override: An explicit :class:`ConversationStore` to use as-is.
        Bypasses all config resolution when not ``None``.
    :param agent: Optional agent; its ``session_config`` is consulted when
        ``config.session`` is absent.
    :returns: A :class:`ConversationStore` instance, or ``None`` when no
        session backend is configured or available.
    """
    if override is not None:
        return override
    config_session = config.session if config is not None else None
    scfg = config_session if config_session is not None else getattr(agent, "session_config", None)
    if scfg is None:
        return None
    try:
        return _build_conversation_store(scfg, ws)
    except (ValueError, ImportError) as exc:
        logger.warning(
            "[tool.apx.agent.session] build failed — no conversation store: %s", exc
        )
        return None


__all__ = [
    "attach_declared_memory",
    "resolve_session_store",
    "resolve_conversation_store",
]
