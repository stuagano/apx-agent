"""Dev UI setup routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    AgentNodeInfo,
    AgentPatternResponse,
    Any,
    ApplyInstructionsRequest,
    ComposeRequest,
    GenerateInstructionsRequest,
    HTMLResponse,
    ProbeResult,
    Request,
    Response,
    SaveAgentsRequest,
    SetAgentPatternRequest,
    SetupInstructionsResponse,
    SetupSaveRequest,
    ToolFromDescriptionRequest,
    ToolFromDescriptionResponse,
    ToolInfo,
    VsIndexInfo,
    WarehouseInfo,
    WireAgentRequest,
    WireAgentResponse,
    WorkspaceClient,
    WorkspaceContextResponse,
    _agent_resource_refs,
    _app_url,
    _discover_vs_indexes,
    _experiment_url,
    _extract_schemas_from_source,
    _find_agent_router_path,
    _find_env_path,
    _generate_agent_instructions,
    _parse_agent_nodes,
    _persist_instructions,
    _pick_workspace_defaults,
    _read_env_file,
    _render_setup_ui,
    _validate_probe_url,
    _write_env_file,
    _ws_upload_agent_file,
    asyncio,
    logger,
    os,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/setup", include_in_schema=False)
    async def setup_ui(request: Request) -> HTMLResponse:
        env_path = _find_env_path()
        current = _read_env_file(env_path) if env_path and env_path.exists() else {}
        embed = request.query_params.get("embed") == "1"

        # Backfill any missing catalog/schema/warehouse with a workspace probe so a
        # fresh agent's Setup page lands pre-populated with the most relevant items
        # from the user's workspace, rather than three "Loading…" dropdowns the
        # user has to scan manually. .env values always win (user-curated wins).
        if not (current.get("DEMO_CATALOG")
                and current.get("DEMO_SCHEMA")
                and current.get("WAREHOUSE_ID")):
            from apx_agent._defaults import _ws_prefer_obo

            try:
                # Probe as the caller (#627): a suggestion the signed-in user
                # cannot actually read is worse than no suggestion. On Apps
                # without OBO this raises and the page renders unseeded rather
                # than proposing App-SP-visible resources.
                ws: WorkspaceClient = _ws_prefer_obo(request)
                picked = await asyncio.to_thread(_pick_workspace_defaults, ws)
                for k, v in picked.items():
                    current.setdefault(k, v)
            except Exception:
                # Best-effort — the page still works with empty defaults, the user
                # just picks manually like before. No crash on probe failure.
                pass

        return HTMLResponse(_render_setup_ui(current, embed=embed))

    @router.get("/_apx/workspace-context", response_model=WorkspaceContextResponse)
    async def workspace_context(request: Request, response: Response) -> Any:
        """Return workspace identity + agent resource summary for the Context tab."""
        response.headers["Cache-Control"] = "no-store"
        import asyncio as _asyncio

        from apx_agent._defaults import _ws_prefer_obo

        ws: WorkspaceClient = _ws_prefer_obo(request)
        ctx = request.app.state.agent_context

        # Workspace identity
        host = (getattr(getattr(ws, "config", None), "host", None) or "").rstrip("/")
        user_id: str | None = None
        try:
            me = await _asyncio.to_thread(ws.current_user.me)
            user_name = getattr(me, "user_name", None) or getattr(me, "display_name", None) or "unknown"
            verified_id = getattr(me, "id", None)
            if isinstance(verified_id, str) and verified_id.strip():
                user_id = verified_id.strip()
        except Exception:
            user_name = "unknown"

        # Use the resolved deployment override before the project's declared catalog.
        # Keep this separate from used_catalogs, which describes actual resource refs.
        from apx_agent._ui_grounding import resolve_project_root
        from apx_agent.cli import _read_apx_agent_config

        import hashlib

        root = resolve_project_root()
        project_id = hashlib.sha256(str(root.resolve()).encode()).hexdigest() if root else None
        configured_catalog = os.environ.get("APX_CATALOG")
        project_catalog = configured_catalog.strip() if configured_catalog else None
        if not project_catalog or "$" in project_catalog:
            declared = _read_apx_agent_config(root / "pyproject.toml").get("catalog") if root else None
            project_catalog = declared.strip() if isinstance(declared, str) else None
        if project_catalog and "$" in project_catalog:
            project_catalog = None

        resources = _agent_resource_refs(ctx)

        # Catalogs + schemas the agent explicitly uses (extract from resource identifiers)
        uc_ids = [
            r["identifier"] for r in resources
            if r["kind"] in ("uc_table", "uc_schema", "uc_function")
            and "." in r["identifier"]
        ]
        used_catalogs: list[str] = sorted({i.split(".")[0] for i in uc_ids})
        used_schemas: list[str] = sorted({
            ".".join(i.split(".")[:2]) for i in uc_ids if i.count(".") >= 1
        })

        return {
            "host": host,
            "user": user_name,
            "user_id": user_id,
            "project_catalog": project_catalog if project_catalog else None,
            "project_id": project_id,
            "resources": resources,
            "used_catalogs": used_catalogs,
            "used_schemas": used_schemas,
            "app_url": _app_url(host, os.environ.get("DATABRICKS_APP_NAME")),
            "experiment_url": _experiment_url(host, os.environ.get("MLFLOW_EXPERIMENT_ID")),
            # A durable host may run without an AgentContext; its model is the App's APX_MODEL.
            "model": ctx.config.model if ctx is not None else os.environ.get("APX_MODEL"),
            "session_store": os.environ.get("AGENT_SESSION_STORE"),
            "memory_store": os.environ.get("AGENT_MEMORY_STORE"),
        }

    @router.get("/_apx/setup/catalogs", response_model=list[str])
    async def setup_catalogs(request: Request) -> Any:
        """List catalogs the caller can see.

        Setup inventory is user-scoped like Discover (#627): reads run under the
        caller's OBO token and fail closed (401) on Apps without one, so the
        dropdowns never offer App-SP-visible catalogs the user cannot read.
        """
        from fastapi.responses import JSONResponse

        from apx_agent._defaults import _ws_prefer_obo

        ws: WorkspaceClient = _ws_prefer_obo(request)
        try:
            cats = [c.name for c in ws.catalogs.list() if c.name]
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return sorted(cats)

    @router.get("/_apx/setup/schemas", response_model=list[str])
    async def setup_schemas(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        from apx_agent._defaults import _ws_prefer_obo

        catalog = request.query_params.get("catalog", "")
        if not catalog:
            return []
        ws: WorkspaceClient = _ws_prefer_obo(request)
        try:
            schemas = [s.name for s in ws.schemas.list(catalog_name=catalog) if s.name
                       and s.name not in ("information_schema",)]
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return sorted(schemas)

    @router.get("/_apx/setup/tables", response_model=list[str])
    async def setup_tables(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import asyncio as _asyncio

        from apx_agent._defaults import _ws_prefer_obo

        catalog = request.query_params.get("catalog", "")
        schema = request.query_params.get("schema", "")
        if not catalog or not schema:
            return []
        ws: WorkspaceClient = _ws_prefer_obo(request)
        try:
            tables = await _asyncio.to_thread(
                lambda: [t.name for t in ws.tables.list(catalog_name=catalog, schema_name=schema) if t.name]
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return sorted(tables)

    @router.get("/_apx/setup/warehouses", response_model=list[WarehouseInfo])
    async def setup_warehouses(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import asyncio as _asyncio

        from apx_agent._defaults import _ws_prefer_obo

        ws: WorkspaceClient = _ws_prefer_obo(request)
        try:
            whs = await _asyncio.to_thread(lambda: [
                {"id": w.id, "name": w.name or w.id, "state": getattr(w.state, "value", str(w.state))}
                for w in ws.warehouses.list() if w.id
            ])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return whs

    @router.get("/_apx/setup/agents", response_model=list[AgentNodeInfo])
    async def setup_agents(request: Request) -> Any:
        """Return the agents defined in the LOCAL agent.py — the file the editor
        edits and the runtime imports — as ``[{name, tools, instructions, wrapper}]``.

        (Previously this discovered deployed *workspace* apps, which is the wrong
        data for the composer: it edits this project's agents, not other apps.)
        """
        path = _find_agent_router_path()
        if not path or not path.exists():
            return []
        return _parse_agent_nodes(path.read_text())

    @router.post("/_apx/setup", response_model=SetupInstructionsResponse)
    async def save_setup(request: Request, body: SetupSaveRequest) -> Any:
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse

        catalog: str = body.catalog.strip()
        schema: str = body.schema_.strip()
        wh_id: str = body.warehouse_id.strip()
        if not catalog or not schema or not wh_id:
            return JSONResponse({"ok": False, "error": "catalog, schema, and warehouse_id required"})

        env_path = _find_env_path()
        if env_path is None:
            return JSONResponse({"ok": False, "error": "Could not find project root"})

        updates = {"DEMO_CATALOG": catalog, "DEMO_SCHEMA": schema, "WAREHOUSE_ID": wh_id}
        _write_env_file(env_path, updates)

        # Persist to workspace so the config survives restarts and redeployments.
        ctx: AgentContext | None = request.app.state.agent_context
        ws: WorkspaceClient = request.app.state.workspace_client
        app_name = ctx.config.name if ctx else None
        if app_name:
            try:
                app_info = await _asyncio.to_thread(lambda: ws.apps.get(app_name))
                ws_source = getattr(app_info, "default_source_code_path", None)
                if ws_source:
                    ws_env_path = ws_source.rstrip("/") + "/.env"
                    env_content = env_path.read_text() if env_path.exists() else ""
                    await _asyncio.to_thread(
                        lambda: ws.workspace.upload(
                            ws_env_path,
                            env_content.encode(),
                            overwrite=True,
                        )
                    )
            except Exception:
                pass  # workspace write is best-effort; local write already succeeded

        instructions: str | None = None
        if body.generate_instructions:
            ctx: AgentContext | None = request.app.state.agent_context
            ws: WorkspaceClient = request.app.state.workspace_client
            instructions = await _generate_agent_instructions(ws, ctx, catalog, schema, wh_id)
            _persist_instructions(ctx, instructions, ws_client=ws)

        return {"ok": True, "instructions": instructions}

    @router.post("/_apx/setup/generate-instructions", response_model=SetupInstructionsResponse)
    async def regen_instructions(request: Request, body: GenerateInstructionsRequest) -> Any:
        from fastapi.responses import JSONResponse
        ctx: AgentContext | None = request.app.state.agent_context
        ws: WorkspaceClient = request.app.state.workspace_client
        try:
            instructions = await _generate_agent_instructions(
                ws, ctx, body.catalog, body.schema_, body.warehouse_id,
            )
        except Exception as exc:  # graceful error instead of a bubbled 500 (#280)
            logger.exception("generate-instructions failed")
            return JSONResponse(
                {"ok": False, "error": f"Instruction generation failed: {exc}"}, status_code=500
            )
        _persist_instructions(ctx, instructions, ws_client=ws)
        return {"ok": True, "instructions": instructions}

    @router.post("/_apx/setup/apply-instructions", response_model=dict[str, Any])
    async def apply_instructions(request: Request, body: ApplyInstructionsRequest) -> Any:
        from fastapi.responses import JSONResponse

        new_instructions: str = body.instructions.strip()
        if not new_instructions:
            return JSONResponse({"ok": False, "error": "No instructions provided"})

        ctx: AgentContext | None = request.app.state.agent_context
        ws_client: WorkspaceClient | None = getattr(request.app.state, "workspace_client", None)
        _persist_instructions(ctx, new_instructions, ws_client=ws_client)
        return {"ok": True}

    @router.get("/_apx/setup/tools", response_model=list[ToolInfo])
    async def setup_list_tools() -> Any:
        path = _find_agent_router_path()
        if not path or not path.exists():
            return []
        schemas = _extract_schemas_from_source(path.read_text())
        result = []
        for s in schemas:
            if s.get("_error"):
                continue
            props = s.get("parameters", {}).get("properties", {})
            result.append({
                "name": s["name"],
                "description": s.get("description", ""),
                "params": [{"name": k, "type": v.get("type", "string")} for k, v in props.items()],
            })
        return result

    @router.post("/_apx/setup/create-tool", response_model=ToolFromDescriptionResponse)
    async def setup_create_tool(request: Request, body: ToolFromDescriptionRequest) -> Any:
        from fastapi.responses import JSONResponse
        import httpx

        desc: str = body.description.strip()
        if not desc:
            return JSONResponse({"ok": False, "error": "No description provided"}, status_code=400)

        # Chain suggest → new via ASGI transport so we reuse all existing logic.
        transport = httpx.ASGITransport(app=request.app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            suggest_r = await client.post(
                "/_apx/tools/suggest",
                json={"prompt": desc},
                headers={k: v for k, v in request.headers.items() if k.lower() != "content-length"},
                timeout=60.0,
            )
            suggest_data = suggest_r.json()
            if not suggest_data.get("ok"):
                return JSONResponse(suggest_data)

            spec: dict = suggest_data["spec"]
            new_r = await client.post(
                "/_apx/tools/new",
                json=spec,
                headers={k: v for k, v in request.headers.items() if k.lower() != "content-length"},
                timeout=15.0,
            )
            new_data = new_r.json()
            if not new_data.get("ok"):
                return JSONResponse(new_data)

        return {"ok": True, "tool_name": spec.get("name", "")}

    @router.post("/_apx/wizard/generate-tools", response_model=ToolFromDescriptionResponse)
    async def wizard_generate_tools(request: Request, body: ToolFromDescriptionRequest) -> Any:
        from fastapi.responses import JSONResponse
        import httpx

        description: str = body.description.strip()
        if not description:
            return JSONResponse({"ok": False, "error": "No description provided"}, status_code=400)

        transport = httpx.ASGITransport(app=request.app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            suggest_r = await client.post(
                "/_apx/tools/suggest",
                json={"prompt": description},
                headers={k: v for k, v in request.headers.items() if k.lower() != "content-length"},
                timeout=60.0,
            )
            suggest_data = suggest_r.json()
            if not suggest_data.get("ok"):
                return JSONResponse(suggest_data)

            spec: dict = suggest_data["spec"]
            new_r = await client.post(
                "/_apx/tools/new",
                json=spec,
                headers={k: v for k, v in request.headers.items() if k.lower() != "content-length"},
                timeout=15.0,
            )
            new_data = new_r.json()
            if not new_data.get("ok"):
                return JSONResponse(new_data)

        return {"ok": True, "tool_name": spec.get("name", "")}

    @router.post("/_apx/setup/wire-agent", response_model=WireAgentResponse)
    async def setup_wire_agent(request: Request, body: WireAgentRequest) -> Any:
        from fastapi.responses import JSONResponse
        import json as _json

        behavior: str = body.behavior.strip()
        agent_name: str = (body.agent_name or "agent").strip()
        if not behavior:
            return JSONResponse({"ok": False, "error": "No behavior description provided"}, status_code=400)

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)
        model = getattr(ctx.config, "model", "") or ""
        if not model:
            return JSONResponse({"ok": False, "error": "No model configured"}, status_code=400)

        path = _find_agent_router_path()
        tool_list: list[dict[str, str]] = []
        if path and path.exists():
            for s in _extract_schemas_from_source(path.read_text()):
                if not s.get("_error"):
                    tool_list.append({"name": s["name"], "description": s.get("description", "")})

        if not tool_list:
            return {"ok": True, "tools": [], "instructions": f"You are {agent_name}. {behavior}"}

        tool_names = [t["name"] for t in tool_list]
        tool_summary = "\n".join(f"- {t['name']}: {t['description']}" for t in tool_list)

        system_msg = (
            "You are helping configure an AI agent. "
            "Given a behavior description and a list of available tools, "
            "select the most relevant tools and write brief agent instructions.\n"
            "Output ONLY a JSON object with exactly two fields:\n"
            '  "tools": array of tool names from the provided list only\n'
            '  "instructions": one paragraph of agent instructions\n'
            "Do not include any tool not in the provided list. Output only valid JSON."
        )
        user_content = (
            f"Agent name: {agent_name}\n"
            f"Behavior: {behavior}\n\n"
            f"Available tools:\n{tool_summary}\n\n"
            "Select tools and write instructions."
        )

        try:
            from databricks_openai import AsyncDatabricksOpenAI
        except ImportError as exc:
            return JSONResponse({"ok": False, "error": f"databricks_openai not available: {exc}"}, status_code=500)

        try:
            llm = AsyncDatabricksOpenAI()
            resp = await llm.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=512,
                temperature=0.0,
            )
            choices = getattr(resp, "choices", None) or []
            raw = ""
            if choices:
                msg = getattr(choices[0], "message", None)
                raw = ((getattr(msg, "content", None) or "") if msg else "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = _json.loads(raw)
            selected = [t for t in (result.get("tools") or []) if t in tool_names]
            instructions = str(result.get("instructions") or behavior)
            return {"ok": True, "tools": selected, "instructions": instructions}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)})

    @router.post("/_apx/setup/agents", response_model=dict[str, Any])
    async def setup_save_agents(request: Request, body: SaveAgentsRequest) -> Any:
        from fastapi.responses import JSONResponse
        import re as _re

        from apx_agent._ui_edit import _parse_agent_nodes, _set_agent_instructions, _set_agent_tools

        nodes_data = body.nodes

        path = _find_agent_router_path()
        if not path or not path.exists():
            return JSONResponse({"ok": False, "error": "agent.py not found"}, status_code=404)

        source = path.read_text()
        known_tools = {
            s["name"] for s in _extract_schemas_from_source(source) if not s.get("_error")
        }
        existing = {n["name"] for n in _parse_agent_nodes(source)}

        # Surgically patch each EXISTING agent's tools + instructions, preserving
        # every other argument (name=, sub_agents=, etc.). New agent names are
        # deferred to the multi-agent composition follow-up rather than appended
        # as orphan, never-referenced Agent(...) lines.
        applied: list[str] = []
        skipped: list[str] = []
        updated = source
        for node_data in nodes_data:
            name = str(node_data.get("name", "")).strip()
            if not name or not _re.match(r"^[a-z_][a-z0-9_]*$", name):
                continue
            if name not in existing:
                skipped.append(name)
                continue
            tools = [t for t in (node_data.get("tools") or []) if t in known_tools]
            instr = str(node_data.get("instructions") or "")
            try:
                updated = _set_agent_tools(updated, tools, target=name)
                updated = _set_agent_instructions(updated, instr, target=name)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    {"ok": False, "error": f"Could not update `{name}`: {exc}"}, status_code=400
                )
            applied.append(name)

        try:
            compile(updated, str(path), "exec")
        except SyntaxError as exc:
            return JSONResponse({"ok": False, "error": f"Generated code has syntax error: {exc}"}, status_code=400)

        if updated != source:
            path.write_text(updated)
            await _ws_upload_agent_file(request, path, updated)

        resp: dict[str, Any] = {
            "ok": True,
            "applied": applied,
            "restart_required": updated != source,
        }
        if skipped:
            resp["skipped"] = skipped
            resp["note"] = (
                "Editing existing agents is live; creating + wiring new agents "
                "(" + ", ".join(skipped) + ") is coming in the multi-agent step."
            )
        return resp

    @router.get("/_apx/setup/probe-json", response_model=ProbeResult)
    async def setup_probe_json(request: Request) -> Any:
        import time as _time
        from fastapi.responses import JSONResponse
        import httpx

        url = request.query_params.get("url", "").strip()
        if not url:
            return JSONResponse({"error": "url parameter required"}, status_code=400)

        # SSRF guard: restrict scheme to http/https and reject hosts resolving
        # to private/loopback/link-local/metadata IPs. follow_redirects=False
        # so a public host can't 302 into the internal network (audit H18).
        reason = _validate_probe_url(url)
        if reason is not None:
            return JSONResponse({"error": reason, "url": url}, status_code=400)

        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                r = await client.get(url)
            latency = int((_time.monotonic() - t0) * 1000)
            return {"status": r.status_code, "latency_ms": latency, "url": url}
        except Exception as exc:
            latency = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({"error": str(exc), "latency_ms": latency, "url": url})

    @router.get("/_apx/setup/vs-indexes", response_model=list[VsIndexInfo])
    async def setup_vs_indexes(request: Request) -> Any:
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse

        from apx_agent._defaults import _ws_prefer_obo

        ws: WorkspaceClient = _ws_prefer_obo(request)
        try:
            indexes = await _asyncio.to_thread(lambda: _discover_vs_indexes(ws))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        # The helper degrades to a single-element ``[{error}]`` (200) when it
        # can't list endpoints; keep that as a JSONResponse so it bypasses the
        # VsIndexInfo response_model instead of tripping response validation.
        if indexes and "error" in indexes[0]:
            return JSONResponse(indexes)
        return indexes

    @router.get("/_apx/setup/agent-pattern", response_model=AgentPatternResponse)
    async def setup_get_agent_pattern() -> Any:
        path = _find_agent_router_path()
        if not path or not path.exists():
            return {"type": "Agent"}
        nodes = _parse_agent_nodes(path.read_text())
        agent_node = next((n for n in nodes if n["name"] == "agent"), None)
        if not agent_node:
            return {"type": "Agent"}
        return {"type": agent_node["wrapper"] or "Agent"}

    @router.post("/_apx/setup/agent-pattern", response_model=dict[str, Any])
    async def setup_set_agent_pattern(request: Request, body: SetAgentPatternRequest) -> Any:
        from fastapi.responses import JSONResponse

        pattern: str = body.pattern.strip()

        _SNIPPET_PATTERNS: dict[str, str] = {
            "SequentialAgent": (
                "agent = SequentialAgent(\n"
                "    agents=[\n"
                "        step1_agent,\n"
                "        step2_agent,\n"
                "    ],\n"
                ")\n"
            ),
            "ParallelAgent": (
                "agent = ParallelAgent(\n"
                "    agents=[\n"
                "        branch1_agent,\n"
                "        branch2_agent,\n"
                "    ],\n"
                ")\n"
            ),
            "RouterAgent": (
                "agent = RouterAgent(\n"
                "    agents={\n"
                "        'route_a': agent_a,\n"
                "        'route_b': agent_b,\n"
                "    },\n"
                "    instructions='Route to the correct specialist.',\n"
                ")\n"
            ),
            "HandoffAgent": (
                "agent = HandoffAgent(\n"
                "    agents={\n"
                "        'specialist_a': agent_a,\n"
                "        'specialist_b': agent_b,\n"
                "    },\n"
                "    instructions='Hand off to the correct specialist.',\n"
                ")\n"
            ),
        }
        if pattern in _SNIPPET_PATTERNS:
            return {"ok": True, "snippet": _SNIPPET_PATTERNS[pattern]}

        # Leaf agent types that can be switched between each other without
        # touching positional args (they all use the same (name, ...) signature).
        _LEAF_AGENT_TYPES = {"Agent", "LlmAgent", "DataAgent", "CoworkerAgent"}
        _AUTO_PATTERNS = _LEAF_AGENT_TYPES | {"LoopAgent"}
        if pattern not in _AUTO_PATTERNS:
            return JSONResponse({"ok": False, "error": f"Unknown pattern: {pattern}"}, status_code=400)

        from apx_agent._ui_edit import _parse_agent_nodes, _set_agent_wrapper

        path = _find_agent_router_path()
        if not path or not path.exists():
            return JSONResponse({"ok": False, "error": "agent.py not found"}, status_code=404)

        source = path.read_text()
        nodes = _parse_agent_nodes(source)
        agent_node = next((n for n in nodes if n["name"] == "agent"), None)
        if not agent_node:
            return JSONResponse({"ok": False, "error": "No 'agent' variable in agent.py"}, status_code=400)

        current_type = agent_node["wrapper"] or "Agent"
        # Leaf→leaf switch is a no-op: DataAgent/CoworkerAgent carry extra positional
        # args (catalog, schema) that _set_agent_wrapper would corrupt by blindly
        # renaming the class. Edit agent.py directly to change between leaf types.
        if current_type == pattern or (pattern in _LEAF_AGENT_TYPES and current_type in _LEAF_AGENT_TYPES):
            return {"ok": True, "type": current_type, "changed": False}

        # Can't collapse a multi-agent composition back to a single agent here —
        # give a specific, actionable message rather than a raw AST error.
        if current_type in ("SequentialAgent", "ParallelAgent", "RouterAgent", "HandoffAgent"):
            members = ", ".join(agent_node.get("members") or [])
            return JSONResponse({
                "ok": False,
                "error": (
                    f"This agent is a {current_type} composing [{members}]. Switching it "
                    f"back to a single {pattern} isn't supported here — edit agent.py in the "
                    f"Editor, or recompose with a different pattern."
                ),
            }, status_code=400)

        # Surgically re-wrap the inner Agent(...) call, preserving all its args
        # (name=, sub_agents=, etc.) instead of regenerating a lossy assignment.
        try:
            updated = _set_agent_wrapper(source, pattern, target="agent")
            compile(updated, str(path), "exec")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"Could not switch pattern: {exc}"}, status_code=400)

        if updated != source:
            path.write_text(updated)
            await _ws_upload_agent_file(request, path, updated)
        return {"ok": True, "type": pattern, "changed": updated != source}

    @router.post("/_apx/setup/compose", response_model=dict[str, Any])
    async def setup_compose(request: Request, body: ComposeRequest) -> Any:
        """Compose ≥2 leaf agents into a workflow root via the chosen pattern.

        Body: ``{pattern, nodes: [{name, tools, instructions, route_key?,
        route_description?}], start?}``. Writes the leaf agents + the workflow
        root into agent.py and adds the wrapper import.
        """
        from fastapi.responses import JSONResponse
        from apx_agent._ui_edit import _compose_agents

        pattern: str = body.pattern.strip()
        nodes = body.nodes
        start: str | None = body.start or None

        # Leaves = the named agents (everything but the reserved root wrapper).
        leaves = [n for n in nodes if str(n.get("name", "")).strip() and n.get("name") != "agent"]

        path = _find_agent_router_path()
        if not path or not path.exists():
            return JSONResponse({"ok": False, "error": "agent.py not found"}, status_code=404)

        source = path.read_text()
        try:
            updated = _compose_agents(source, pattern, leaves, start=start)
            compile(updated, str(path), "exec")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        if updated != source:
            path.write_text(updated)
            await _ws_upload_agent_file(request, path, updated)
        return {
            "ok": True,
            "type": pattern,
            "agents": [leaf["name"] for leaf in leaves],
            "changed": updated != source,
            "restart_required": updated != source,
        }

    @router.get("/_apx/wizard", include_in_schema=False)
    async def wizard_ui() -> Any:
        from starlette.responses import RedirectResponse as _R
        return _R("/_apx/setup", status_code=302)

    @router.get("/_apx/wizard/tables", include_in_schema=False)
    async def wizard_tables(request: Request, catalog: str, schema: str) -> Any:
        from fastapi.responses import JSONResponse
        ws: WorkspaceClient = request.app.state.workspace_client
        warehouse_id = os.environ.get("WAREHOUSE_ID") or ""
        env_path = _find_env_path()
        if env_path and env_path.exists():
            env_vars = _read_env_file(env_path)
            warehouse_id = warehouse_id or env_vars.get("WAREHOUSE_ID", "")
        tables: list[dict[str, Any]] = []
        try:
            uc_tables = list(ws.tables.list(catalog_name=catalog, schema_name=schema))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        for t in uc_tables[:20]:
            tname = t.name or ""
            cols: list[dict[str, str]] = []
            row_count: int | None = None
            try:
                detail = ws.tables.get(f"{catalog}.{schema}.{tname}")
                if detail.columns:
                    cols = [
                        {"name": c.name or "", "type": (c.type_text or c.type_name.value if c.type_name else "").lower()}
                        for c in detail.columns
                    ]
                if detail.properties:
                    rc = detail.properties.get("numRows") or detail.properties.get("spark.sql.statistics.numRows")
                    if rc:
                        row_count = int(rc)
            except Exception:
                pass
            tables.append({"name": tname, "columns": cols, "row_count": row_count})
        return JSONResponse({"tables": tables, "warehouse_id": warehouse_id})
