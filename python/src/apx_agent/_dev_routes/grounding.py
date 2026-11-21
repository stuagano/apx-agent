"""Dev UI grounding routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    ColumnDescriptionsRequest,
    ColumnDescriptionsSaveResponse,
    ColumnSuggestRequest,
    ColumnSuggestResponse,
    GroundingColumnsResponse,
    GroundingGenerateRequest,
    GroundingGenerateResponse,
    HTMLResponse,
    MemoryResponse,
    Request,
    _GROUNDING_COLUMNS_CACHE,
    logger,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/grounding")
    async def grounding_ui() -> Any:
        """The field-description curation page (#292) — review + accept/edit
        per-column descriptions for the agent's OKF grounding bundle."""
        from apx_agent._ui_grounding import render_grounding_ui

        return HTMLResponse(render_grounding_ui())

    @router.get("/_apx/knowledge", include_in_schema=False)
    async def knowledge_ui() -> Any:
        """The Knowledge page — displays OKF bundle Functions, Views, and Glossary."""
        from apx_agent._ui_knowledge import render_knowledge_ui

        return HTMLResponse(render_knowledge_ui())

    @router.get("/_apx/grounding/columns", response_model=GroundingColumnsResponse)
    async def grounding_columns(request: Request) -> Any:
        """Per-column current-vs-suggested description curation state for the
        agent's OKF bundle (#292). Suggestions come from Unity Catalog COMMENTs;
        empty ``tables`` plus ``can_generate`` when the project has no pack but
        a DataAgent catalog.schema is known (Generate-pack CTA).

        Result cached for 10 minutes — UC column comments change rarely.
        Cache is busted automatically when columns are saved.
        """
        import asyncio as _asyncio
        from apx_agent._ui_grounding import grounding_columns_payload, resolve_okf_root

        if _GROUNDING_COLUMNS_CACHE.fresh:
            return _GROUNDING_COLUMNS_CACHE.get()

        okf_root = resolve_okf_root()
        ws = getattr(request.app.state, "workspace_client", None)
        ctx = getattr(request.app.state, "agent_context", None)

        cached = _GROUNDING_COLUMNS_CACHE.get()
        if cached is not None and not _GROUNDING_COLUMNS_CACHE._refreshing:
            _GROUNDING_COLUMNS_CACHE._refreshing = True
            async def _refresh_grounding() -> None:
                try:
                    result = await _asyncio.to_thread(grounding_columns_payload, okf_root, ws, ctx)
                    _GROUNDING_COLUMNS_CACHE.put(result)
                except Exception:
                    _GROUNDING_COLUMNS_CACHE._refreshing = False
            _asyncio.create_task(_refresh_grounding())
            return cached

        result = await _asyncio.to_thread(grounding_columns_payload, okf_root, ws, ctx)
        _GROUNDING_COLUMNS_CACHE.put(result)
        return result

    @router.post("/_apx/grounding/columns", response_model=ColumnDescriptionsSaveResponse)
    async def save_grounding_columns(
        request: Request, body: ColumnDescriptionsRequest
    ) -> Any:
        """Write accepted column descriptions into the OKF bundle (#292). Edits
        the LOCAL bundle only — never Unity Catalog. Token-gated by the dev
        write guard like every other write route."""
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse
        from apx_agent._ui_grounding import apply_column_descriptions, resolve_okf_root

        okf_root = resolve_okf_root()
        if okf_root is None:
            return JSONResponse({"ok": False, "error": "No .apx/okf bundle found"}, status_code=404)
        modified = await _asyncio.to_thread(apply_column_descriptions, okf_root, body.accepted)
        _GROUNDING_COLUMNS_CACHE._value = None  # bust so next GET re-reads from OKF
        return {"ok": True, "modified": modified}

    @router.post("/_apx/grounding/generate", response_model=GroundingGenerateResponse)
    async def generate_grounding_pack(
        request: Request, body: GroundingGenerateRequest
    ) -> Any:
        """Emit an OKF pack from the DataAgent's catalog.schema via the UC Tables
        API, wire ``knowledge = "./.apx/okf"``, and seed descriptions from UC
        COMMENTs. Token-gated like other write routes; restart required to load
        grounding into the live agent."""
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse
        from apx_agent._ui_grounding import (
            generate_okf_pack,
            resolve_data_source_for_grounding,
            resolve_project_root,
        )

        ws = getattr(request.app.state, "workspace_client", None)
        if ws is None:
            return JSONResponse(
                {"ok": False, "error": "No workspace client available"},
                status_code=400,
            )
        ctx = getattr(request.app.state, "agent_context", None)
        catalog = (body.catalog or "").strip()
        schema = (body.schema_ or "").strip()
        if not (catalog and schema):
            pair = resolve_data_source_for_grounding(ctx)
            if not pair:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "No catalog.schema found — set APX_CATALOG/APX_SCHEMA "
                        "or point agent.py at a DataAgent",
                    },
                    status_code=400,
                )
            catalog, schema = pair
        root = resolve_project_root()
        try:
            result = await _asyncio.to_thread(
                generate_okf_pack,
                ws,
                catalog,
                schema,
                project_root=root,
                force=body.force,
            )
        except ValueError as e:
            msg = str(e)
            code = 409 if "already exists" in msg else 400
            return JSONResponse({"ok": False, "error": msg}, status_code=code)
        except Exception as e:  # noqa: BLE001
            logger.warning("generate_okf_pack failed: %s", e)
            return JSONResponse(
                {"ok": False, "error": f"Failed to generate pack: {e}"},
                status_code=500,
            )
        return {
            "ok": True,
            "catalog": result["catalog"],
            "schema": result["schema"],
            "table_count": result["table_count"],
            "knowledge_wired": result["knowledge_wired"],
            "restart_required": result.get("restart_required", True),
        }

    @router.post("/_apx/grounding/suggest", response_model=ColumnSuggestResponse)
    async def suggest_grounding_descriptions(
        request: Request, body: ColumnSuggestRequest
    ) -> Any:
        """LLM-generate column descriptions for a table (#292 phase C) — an AI
        suggestion source alongside UC comments. Reads nothing from / writes
        nothing to the bundle; the UI offers the result for accept like any
        other suggestion."""
        from fastapi.responses import JSONResponse
        from apx_agent._okf import okf_columns
        from apx_agent._ui_grounding import generate_column_descriptions, resolve_okf_root

        ctx: AgentContext | None = request.app.state.agent_context
        model = (getattr(ctx.config, "model", "") or "") if ctx else ""
        if not model:
            return JSONResponse({"ok": False, "error": "No model configured"}, status_code=400)
        okf_root = resolve_okf_root()
        if okf_root is None:
            return JSONResponse({"ok": False, "error": "No .apx/okf bundle found"}, status_code=404)
        rows = okf_columns(okf_root).get(body.table, [])
        suggestions = await generate_column_descriptions(model, body.table, rows)
        return {"ok": True, "suggestions": suggestions}

    @router.get("/_apx/memories", response_model=list[MemoryResponse])
    async def list_memories(request: Request) -> Any:
        """Return the most recent stored memories for the landing page preview."""
        import asyncio as _asyncio

        ctx = request.app.state.agent_context
        if ctx is None:
            return []
        agent = ctx.agent
        store = getattr(agent, "_apx_memory_store", None)
        if store is None:
            return []
        principal = getattr(agent, "_apx_memory_principal", None) or ""
        namespace = getattr(agent, "_apx_memory_namespace", None)
        try:
            from apx_agent._memory import MemoryFilter
            filt = MemoryFilter(principal_id=principal, namespace=namespace, limit=5)
            rows = await _asyncio.to_thread(store.list, filt)
            rows = sorted(rows, key=lambda m: m.updated_at, reverse=True)[:5]
            # Raw list (not JSONResponse) so ``response_model`` validates rows.
            return [
                {
                    "id": m.id,
                    "content": m.content,
                    "namespace": m.namespace,
                    "updated_at": m.updated_at,
                }
                for m in rows
            ]
        except Exception:
            # No 503 here: the landing preview degrades to an empty list (the
            # pre-existing behaviour) rather than surfacing an error state.
            logger.exception("/_apx/memories: list failed")
            return []
