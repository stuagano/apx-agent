"""Dev UI tools routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    HTMLResponse,
    JSONResponse,
    Request,
    ToolDeleteResponse,
    ToolNewRequest,
    ToolSchemaResponse,
    ToolSuggestRequest,
    ToolSuggestResponse,
    ToolsSaveRequest,
    _build_apx_openapi_spec,
    _build_tool_function,
    _extract_schemas_from_source,
    _find_agent_router_path,
    _fix_sql_identifiers,
    _mine_schema_from_source,
    _remove_tool,
    _render_tools_ui,
    _splice_tool,
    _tools_payload,
    _ws_upload_agent_file,
    save_tool_source,
    tool_file_response,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""
    api_prefix = ctx.api_prefix


    @router.get("/_apx/tools", include_in_schema=False)
    async def tools_dev_ui() -> HTMLResponse:
        """Tool inspector — the real source of every registered tool, editable."""
        return HTMLResponse(_render_tools_ui())

    @router.get("/_apx/tools/list", include_in_schema=False)
    async def tools_list(request: Request) -> Any:
        return JSONResponse(_tools_payload(request))

    @router.get("/_apx/tools/file", include_in_schema=False)
    async def tools_file(request: Request, name: str | None = None) -> Any:
        tool_file = tool_file_response(request, name)
        if tool_file.contents is None:
            return JSONResponse(
                {"ok": False, "error": "No tool source is on disk in this runtime"},
                status_code=404,
            )
        from fastapi.responses import Response as _Response

        safe_name = (tool_file.filename or "tools.py").replace('"', "_")
        return _Response(
            tool_file.contents,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    @router.post("/_apx/tools/save", include_in_schema=False)
    async def tools_save(request: Request, body: ToolsSaveRequest) -> Any:
        """Rewrite one registered tool function in place. Protected by the
        router-level _dev_write_guard (Apps SSO / APX_DEV_UI_TOKEN)."""
        result = save_tool_source(request, body)
        return JSONResponse(result.payload, status_code=result.status)

    @router.get("/_apx/openapi.json", response_model=dict[str, Any])
    async def apx_openapi_spec(request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        # base_url goes into the openapi `servers` field so Scalar uses the
        # right host in its curl examples. Prefer X-Forwarded-* headers (set
        # by reverse proxies like the Databricks Apps gateway) over
        # request.base_url, which reflects the internal port when uvicorn
        # isn't started with --proxy-headers.
        fwd_host = request.headers.get("x-forwarded-host")
        if fwd_host:
            fwd_proto = request.headers.get("x-forwarded-proto", "https")
            base_url = f"{fwd_proto}://{fwd_host}"
        else:
            base_url = str(request.base_url)
        return _build_apx_openapi_spec(ctx, api_prefix, base_url=base_url)

    @router.get("/_apx/tools/schema", response_model=ToolSchemaResponse)
    async def get_tool_schema_context(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import asyncio
        import sys as _sys

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"})

        _ar_mod = next(
            (m for n, m in _sys.modules.items() if n.endswith(".backend.agent_router")),
            None,
        )
        catalog: str = getattr(_ar_mod, "CATALOG", "") if _ar_mod else ""
        schema: str = getattr(_ar_mod, "SCHEMA", "") if _ar_mod else ""
        warehouse_id: str = getattr(_ar_mod, "WAREHOUSE_ID", "") if _ar_mod else ""

        if not catalog or not schema or not warehouse_id:
            return JSONResponse({"ok": False, "error": "CATALOG/SCHEMA/WAREHOUSE_ID not set in agent_router"})

        ws_client = request.app.state.workspace_client

        def _query(wh_id: str) -> list[dict[str, Any]]:
            from databricks.sdk.service.sql import StatementParameterListItem

            resp = ws_client.statement_execution.execute_statement(
                warehouse_id=wh_id,
                statement=(
                    "SELECT table_name, column_name, data_type, ordinal_position "
                    "FROM information_schema.columns "
                    "WHERE table_schema = :schema "
                    "ORDER BY table_name, ordinal_position"
                ),
                parameters=[StatementParameterListItem(name="schema", value=schema, type="STRING")],
                catalog=catalog,
                schema=schema,
            )
            if not resp.result or not resp.result.data_array:
                return []
            cols = [c.name for c in resp.manifest.schema.columns]
            return [{c: v for c, v in zip(cols, row)} for row in resp.result.data_array]

        def _query_with_fallback() -> list[dict[str, Any]]:
            try:
                return _query(warehouse_id)
            except Exception:
                for wh in ws_client.warehouses.list():
                    if wh.id:
                        try:
                            return _query(wh.id)
                        except Exception:
                            continue
                raise RuntimeError(f"No accessible warehouse found (configured: {warehouse_id})")

        try:
            rows = await asyncio.to_thread(_query_with_fallback)
        except Exception:
            rows = []

        if rows:
            tables: dict[str, list[dict[str, str]]] = {}
            for r in rows:
                t = r["table_name"]
                tables.setdefault(t, []).append({"name": r["column_name"], "type": r["data_type"]})
            return {"ok": True, "catalog": catalog, "schema": schema, "tables": tables}

        path = _find_agent_router_path()
        if path and path.exists():
            mined = _mine_schema_from_source(path.read_text())
            if mined:
                tables_fmt = {
                    t: [{"name": c.split("(")[0], "type": c.split("(")[1].rstrip(")")}
                        for c in cols]
                    for t, cols in mined.items()
                }
                return {
                    "ok": True, "catalog": catalog, "schema": schema,
                    "tables": tables_fmt, "source": "mined",
                }

        return {"ok": True, "catalog": catalog, "schema": schema, "tables": {}}

    @router.post("/_apx/tools/suggest", response_model=ToolSuggestResponse)
    async def suggest_tool_spec(request: Request, body: ToolSuggestRequest) -> Any:
        from fastapi.responses import JSONResponse
        import json as _json
        from httpx import AsyncClient

        prompt: str = body.prompt.strip()
        if not prompt:
            return JSONResponse({"ok": False, "error": "No description provided"})

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"})

        path = _find_agent_router_path()
        existing_ctx = ""
        source_text = ""
        if path and path.exists():
            source_text = path.read_text()
            schemas = _extract_schemas_from_source(source_text)
            lines = []
            for s in schemas:
                if s.get("_error"):
                    continue
                props = s.get("parameters", {}).get("properties", {})
                param_str = ", ".join(f"{k}: {v.get('type', 'str')}" for k, v in props.items())
                lines.append(f"def {s['name']}({param_str})  # {s.get('description', '')}")
            existing_ctx = "\n".join(lines)

        import asyncio as _asyncio
        import sys as _sys2
        _ar_mod = next(
            (m for n, m in _sys2.modules.items() if n.endswith(".backend.agent_router")),
            None,
        )
        uc_catalog = getattr(_ar_mod, "CATALOG", "") if _ar_mod else ""
        uc_schema = getattr(_ar_mod, "SCHEMA", "") if _ar_mod else ""
        uc_warehouse = getattr(_ar_mod, "WAREHOUSE_ID", "") if _ar_mod else ""
        table_schema_ctx = ""
        fetched_tables: dict[str, list[str]] = {}
        if uc_catalog and uc_schema and uc_warehouse:
            def _fetch_schemas() -> dict[str, list[str]]:
                ws_client = request.app.state.workspace_client
                def _do(wh_id: str) -> dict[str, list[str]]:
                    from databricks.sdk.service.sql import StatementParameterListItem

                    resp = ws_client.statement_execution.execute_statement(
                        warehouse_id=wh_id,
                        statement=(
                            "SELECT table_name, column_name, data_type "
                            "FROM information_schema.columns "
                            "WHERE table_schema = :schema "
                            "ORDER BY table_name, ordinal_position"
                        ),
                        parameters=[
                            StatementParameterListItem(name="schema", value=uc_schema, type="STRING")
                        ],
                        catalog=uc_catalog,
                        schema=uc_schema,
                    )
                    if not resp.result or not resp.result.data_array:
                        return {}
                    col_names = [c.name for c in resp.manifest.schema.columns]
                    result: dict[str, list[str]] = {}
                    for row in resp.result.data_array:
                        r = dict(zip(col_names, row))
                        t = r["table_name"]
                        result.setdefault(t, []).append(f"{r['column_name']}({r['data_type']})")
                    return result
                try:
                    return _do(uc_warehouse)
                except Exception:
                    ws_client = request.app.state.workspace_client
                    for wh in ws_client.warehouses.list():
                        if wh.id:
                            try:
                                return _do(wh.id)
                            except Exception:
                                continue
                    return {}
            try:
                fetched_tables = await _asyncio.to_thread(_fetch_schemas)
            except Exception:
                pass

        if not fetched_tables and path and path.exists():
            fetched_tables = _mine_schema_from_source(source_text or path.read_text())

        if fetched_tables:
            table_schema_ctx = "\n".join(
                f"  {t}: {', '.join(cols[:10])}"
                for t, cols in fetched_tables.items()
            )

        system_msg = (
            "You are a Python tool scaffolder for an AI agent. "
            "Given a description of a new tool, output a JSON object with these exact fields:\n"
            '  "name": snake_case Python function name\n'
            '  "description": one sentence shown to the LLM (what it does and when to call it)\n'
            '  "params": array of {"name": str, "type": str, "desc": str} — only user-visible params, never ws/workspace\n'
            '  "returns": Python return type: str, list[str], dict[str, Any], list[dict[str, Any]], int, float, or bool\n'
            '  "body": complete indented Python function body (4-space indent)\n\n'
            "For the body, use _run_sql(ws, sql) to query the database and "
            "_cast_numerics(row) to cast numeric strings. "
            "Use f-strings for SQL. Always check `if rows and 'error' in rows[0]` before returning. "
            "Match the naming and style of the existing tools. "
            "IMPORTANT: use ONLY the exact table names and column names listed in 'Available tables' below — "
            "do not invent or guess names. "
            "Output ONLY valid JSON — no markdown fences, no explanation."
        )
        user_content = f"Agent instructions:\n{ctx.config.instructions}\n\n"
        if existing_ctx:
            user_content += f"Existing tool signatures:\n{existing_ctx}\n\n"
        if table_schema_ctx:
            user_content += f"Available tables ({uc_schema}):\n{table_schema_ctx}\n\n"
        user_content += f"New tool description:\n{prompt}"

        ws_client = request.app.state.workspace_client
        auth_headers = ws_client.config.authenticate()
        endpoint_url = (
            f"{ws_client.config.host.rstrip('/')}"
            f"/serving-endpoints/{ctx.config.model}/invocations"
        )

        async with AsyncClient() as client:
            r = await client.post(
                endpoint_url,
                headers={**auth_headers, "Content-Type": "application/json"},
                json={
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": 1024,
                    "temperature": 0.0,
                },
                timeout=30.0,
            )
            r.raise_for_status()
            data = r.json()

        raw: str = data["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        try:
            spec = _json.loads(raw)
        except Exception:
            return JSONResponse({"ok": False, "error": "Model returned non-JSON — try rephrasing"})

        if fetched_tables and spec.get("body"):
            spec["body"] = _fix_sql_identifiers(spec["body"], fetched_tables)

        return {"ok": True, "spec": spec}

    @router.post("/_apx/tools/new", response_model=dict[str, Any])
    async def create_new_tool(request: Request, body: ToolNewRequest) -> Any:
        from fastapi.responses import JSONResponse
        import re as _re

        name: str = _re.sub(r"\W", "_", (body.name or "").strip()) or "my_tool"
        description: str = (body.description or "").strip()
        params: list[dict[str, Any]] = [
            p for p in body.params if p.get("name", "").strip()
        ]
        returns: str = body.returns or "str"
        fn_body: str | None = body.body or None
        target: str = (body.agent or "agent").strip() or "agent"

        path = _find_agent_router_path()
        if not path:
            return JSONResponse({"ok": False, "error": "agent.py not found"})

        source = path.read_text()
        _m = _re.search(r"^(\w+)\s*=\s*Dependencies\.Client", source, _re.MULTILINE)
        ws_type = _m.group(1) if _m else "AppClient"

        fn_code = _build_tool_function(name, description, params, returns, ws_type, body=fn_body)
        updated = _splice_tool(source, fn_code, name, target=target)

        try:
            compile(updated, str(path), "exec")
        except SyntaxError as e:
            return JSONResponse({"ok": False, "error": f"Syntax error at line {e.lineno}: {e.msg}"})

        path.write_text(updated)
        await _ws_upload_agent_file(request, path, updated)

        # Honest reporting: if the agent is a composition, the new tool is defined
        # but not attached to any single agent — tell the user to pick a leaf.
        from apx_agent._ui_edit import _parse_agent_nodes
        nodes = _parse_agent_nodes(updated)
        wired = any(name in (n.get("tools") or []) for n in nodes)
        if not wired:
            leaves = [n["name"] for n in nodes if n["name"] != "agent" and n.get("wrapper") is None]
            return {
                "ok": True, "wired": False, "agents": leaves, "restart_required": True,
                "note": (
                    f"`{name}` was added to agent.py but not attached to an agent "
                    f"(this agent is composed). Re-add it choosing one of: {', '.join(leaves) or '(define a leaf agent first)'}."
                ),
            }
        return {"ok": True, "wired": True, "restart_required": True}

    @router.delete("/_apx/tools/{fn_name}", response_model=ToolDeleteResponse)
    async def delete_tool(fn_name: str, request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import re as _re

        path = _find_agent_router_path()
        if not path:
            return JSONResponse({"ok": False, "error": "agent_router.py not found"})

        source = path.read_text()
        if not _re.search(rf'^def {_re.escape(fn_name)}\b', source, _re.MULTILINE):
            return JSONResponse({"ok": False, "error": f"Tool '{fn_name}' not found"})

        updated = _remove_tool(source, fn_name)
        try:
            compile(updated, "agent_router.py", "exec")
        except SyntaxError as e:
            return JSONResponse({"ok": False, "error": f"Syntax error after removal at line {e.lineno}: {e.msg}"})

        path.write_text(updated)
        await _ws_upload_agent_file(request, path, updated)
        return {"ok": True}
