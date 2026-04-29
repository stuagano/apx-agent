"""Dev UI — /_apx/* routes for the agent development experience.

Optional module. Install with ``pip install apx-agent[dev]`` or ``apx-agent[all]``.

Usage::

    from apx_agent._dev import build_dev_ui_router, inject_create_tool_meta

    # In your lifespan:
    inject_create_tool_meta(ctx)

    # Mount on the app:
    app.include_router(build_dev_ui_router())
"""

from __future__ import annotations

import json as _json
import logging
import os
from typing import Any

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._models import AgentContext, AgentTool
from ._ui_chat import _render_agent_ui, _build_apx_openapi_spec
from ._ui_edit import (
    _find_agent_router_path,
    _find_deploy_root,
    _find_evals_path,
    _extract_schemas_from_source,
    _mine_schema_from_source,
    _render_edit_ui,
    _build_tool_function,
    _splice_tool,
    _fix_sql_identifiers,
    _remove_tool,
)
from ._ui_setup import (
    _find_env_path,
    _read_env_file,
    _write_env_file,
    _render_setup_ui,
)
from ._ui_probe import _generate_agent_instructions, _render_probe_ui, _run_probe_checks
from ._ui_nav import _apx_nav_css, _apx_nav_html, _deploy_overlay_html
from ._ui_traces import _render_trace_detail_ui, _render_traces_list_ui
from ._trace import get_trace, get_traces

logger = logging.getLogger(__name__)


def _parse_judge_output(text: str) -> tuple[str, str]:
    """Extract verdict and reason from a judge model's output.

    Expected format::

        VERDICT: PASS
        REASON: Response correctly identifies the answer.

    Tolerant of: missing labels, swapped order, extra prose. Falls back to FAIL
    if PASS isn't clearly indicated, so unclear judges count as failures.
    """
    verdict = "FAIL"
    reason = ""
    if not text:
        return verdict, "No output from judge model"

    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("VERDICT:"):
            tail = stripped.split(":", 1)[1].strip().upper()
            verdict = "PASS" if tail.startswith("PASS") else "FAIL"
        elif upper.startswith("REASON:"):
            reason = stripped.split(":", 1)[1].strip()

    if not reason:
        # No labelled REASON line — use the first non-VERDICT line as the reason.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.upper().startswith("VERDICT"):
                reason = stripped
                break

    # If we never saw a VERDICT line, infer from the text body.
    if verdict == "FAIL" and "VERDICT:" not in text.upper():
        upper = text.upper()
        if "PASS" in upper and "FAIL" not in upper:
            verdict = "PASS"

    return verdict, reason or "(no reason provided)"


def inject_create_tool_meta(ctx: AgentContext) -> None:
    """Inject the create_tool meta-tool for dev mode."""
    _create_tool_meta = AgentTool(
        name="create_tool",
        description=(
            "Create a new tool for this agent from a natural language description. "
            "Call this when the user asks to add a new capability, tool, or function to the agent. "
            "After creation, the tool is live after hot-reload (a few seconds)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What the tool should do.",
                }
            },
            "required": ["description"],
        },
    )
    ctx.tools.append(_create_tool_meta)
    ctx._tool_map["create_tool"] = _create_tool_meta
    _dev_addendum = (
        "\n\n[DEV MODE] You have a special `create_tool` capability. "
        "When the user asks you to add a new tool, capability, or function, "
        "call `create_tool` with a detailed description of what it should do. "
        "The tool will be generated, inserted into agent_router.py, and live after hot-reload."
    )
    ctx.config.instructions = (ctx.config.instructions or "") + _dev_addendum
    logger.info("Dev mode: create_tool meta-tool injected into agent context")


def build_dev_ui_router(api_prefix: str = "/api") -> APIRouter:
    """Build the /_apx/* dev UI routes."""
    router = APIRouter()

    @router.get("/_apx/agent", include_in_schema=False)
    async def agent_dev_ui(request: Request) -> HTMLResponse:
        ctx: AgentContext | None = request.app.state.agent_context
        return HTMLResponse(_render_agent_ui(ctx))

    @router.get("/_apx/tools", include_in_schema=False)
    async def tools_dev_ui() -> Any:
        from starlette.responses import RedirectResponse as _R
        return _R("/_apx/agent", status_code=302)

    @router.get("/_apx/openapi.json", include_in_schema=False)
    async def apx_openapi_spec(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        ctx: AgentContext | None = request.app.state.agent_context
        return JSONResponse(_build_apx_openapi_spec(ctx, api_prefix))

    @router.get("/_apx/probe", include_in_schema=False)
    async def probe_dev_ui() -> HTMLResponse:
        return HTMLResponse(_render_probe_ui())

    @router.get("/_apx/probe/checks", include_in_schema=False)
    async def probe_checks(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        ctx: AgentContext | None = request.app.state.agent_context
        return JSONResponse(await _run_probe_checks(ctx))

    @router.get("/_apx/traces", include_in_schema=False)
    async def traces_list_ui() -> HTMLResponse:
        return HTMLResponse(_render_traces_list_ui(get_traces()))

    @router.get("/_apx/traces/{trace_id}", include_in_schema=False)
    async def trace_detail_ui(trace_id: str) -> HTMLResponse:
        trace = get_trace(trace_id)
        if trace is None:
            return HTMLResponse("Trace not found", status_code=404)
        return HTMLResponse(_render_trace_detail_ui(trace))

    @router.post("/_apx/replay/tool", include_in_schema=False)
    async def replay_tool(request: Request) -> Any:
        """Re-invoke a registered tool with arbitrary args. Used by the
        trace detail view to debug-iterate without restarting a conversation."""
        from fastapi.responses import JSONResponse
        import time as _time
        from httpx import ASGITransport, AsyncClient

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        body = await request.json()
        tool_name = body.get("tool_name", "")
        args = body.get("args", {})
        if not tool_name:
            return JSONResponse({"ok": False, "error": "tool_name is required"}, status_code=400)
        if tool_name not in ctx._tool_map:
            return JSONResponse({"ok": False, "error": f"Tool '{tool_name}' not found"}, status_code=404)

        # Forward OBO headers so workspace-scoped tools work the same way the
        # runner invokes them.
        obo_headers = {
            "Authorization": request.headers.get("Authorization", ""),
            "X-Forwarded-Access-Token": request.headers.get("X-Forwarded-Access-Token", ""),
            "X-Forwarded-Host": request.headers.get("X-Forwarded-Host", ""),
        }
        t0 = _time.monotonic()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=request.app),
                base_url="http://internal",
            ) as client:
                resp = await client.post(
                    f"{api_prefix}/tools/{tool_name}",
                    json=args,
                    headers=obo_headers,
                )
            elapsed = int((_time.monotonic() - t0) * 1000)
            if resp.status_code >= 400:
                return JSONResponse({
                    "ok": False,
                    "error": f"Tool returned {resp.status_code}: {resp.text}",
                    "duration_ms": elapsed,
                }, status_code=200)
            result = resp.json()
            output = result if isinstance(result, str) else _json.dumps(result)
            return JSONResponse({"ok": True, "output": output, "duration_ms": elapsed})
        except Exception as exc:  # noqa: BLE001
            elapsed = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({
                "ok": False,
                "error": str(exc),
                "duration_ms": elapsed,
            }, status_code=200)

    @router.post("/_apx/replay/llm", include_in_schema=False)
    async def replay_llm(request: Request) -> Any:
        """Re-invoke the configured model with edited messages. Returns
        the model's output text — useful for "what if I had asked X instead?"."""
        from fastapi.responses import JSONResponse
        import time as _time

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        body = await request.json()
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return JSONResponse({"ok": False, "error": "messages must be a non-empty list"}, status_code=400)
        model = body.get("model") or getattr(ctx.config, "model", "")
        if not model:
            return JSONResponse({"ok": False, "error": "No model configured"}, status_code=400)

        try:
            from databricks_openai import AsyncDatabricksOpenAI
        except ImportError as exc:
            return JSONResponse({"ok": False, "error": f"databricks_openai not available: {exc}"}, status_code=500)

        t0 = _time.monotonic()
        try:
            client = AsyncDatabricksOpenAI()
            resp = await client.responses.create(model=model, input=messages)
            elapsed = int((_time.monotonic() - t0) * 1000)
            output = getattr(resp, "output_text", "") or ""
            return JSONResponse({"ok": True, "output": output, "duration_ms": elapsed, "model": model})
        except Exception as exc:  # noqa: BLE001
            elapsed = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({
                "ok": False,
                "error": str(exc),
                "duration_ms": elapsed,
            }, status_code=200)

    @router.get("/_apx/edit", include_in_schema=False)
    async def edit_dev_ui(request: Request) -> HTMLResponse:
        path = _find_agent_router_path()
        if not path or not path.exists():
            return HTMLResponse(_render_edit_ui("", not_found=True))
        return HTMLResponse(_render_edit_ui(path.read_text()))

    @router.post("/_apx/edit", include_in_schema=False)
    async def save_agent_router(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        body = await request.json()
        content: str = body.get("content", "")
        try:
            compile(content, "agent_router.py", "exec")
        except SyntaxError as e:
            return JSONResponse({"ok": False, "error": f"Syntax error at line {e.lineno}: {e.msg}"})
        path = _find_agent_router_path()
        if not path:
            return JSONResponse({"ok": False, "error": "agent_router.py not found in running process"})
        path.write_text(content)
        return JSONResponse({"ok": True})

    @router.post("/_apx/edit/preview", include_in_schema=False)
    async def preview_tool_schemas(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        body = await request.json()
        source: str = body.get("source", "")
        return JSONResponse(_extract_schemas_from_source(source))

    @router.get("/_apx/tools/schema", include_in_schema=False)
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
            resp = ws_client.statement_execution.execute_statement(
                warehouse_id=wh_id,
                statement=(
                    f"SELECT table_name, column_name, data_type, ordinal_position "
                    f"FROM information_schema.columns "
                    f"WHERE table_schema = '{schema}' "
                    f"ORDER BY table_name, ordinal_position"
                ),
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
            return JSONResponse({"ok": True, "catalog": catalog, "schema": schema, "tables": tables})

        path = _find_agent_router_path()
        if path and path.exists():
            mined = _mine_schema_from_source(path.read_text())
            if mined:
                tables_fmt = {
                    t: [{"name": c.split("(")[0], "type": c.split("(")[1].rstrip(")")}
                        for c in cols]
                    for t, cols in mined.items()
                }
                return JSONResponse({
                    "ok": True, "catalog": catalog, "schema": schema,
                    "tables": tables_fmt, "source": "mined",
                })

        return JSONResponse({"ok": True, "catalog": catalog, "schema": schema, "tables": {}})

    @router.post("/_apx/tools/suggest", include_in_schema=False)
    async def suggest_tool_spec(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import json as _json
        from httpx import AsyncClient

        body = await request.json()
        prompt: str = body.get("prompt", "").strip()
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
                    resp = ws_client.statement_execution.execute_statement(
                        warehouse_id=wh_id,
                        statement=(
                            f"SELECT table_name, column_name, data_type "
                            f"FROM information_schema.columns "
                            f"WHERE table_schema = '{uc_schema}' "
                            f"ORDER BY table_name, ordinal_position"
                        ),
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

        return JSONResponse({"ok": True, "spec": spec})

    @router.post("/_apx/tools/new", include_in_schema=False)
    async def create_new_tool(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import re as _re

        req_body = await request.json()
        name: str = _re.sub(r"\W", "_", req_body.get("name", "").strip()) or "my_tool"
        description: str = req_body.get("description", "").strip()
        params: list[dict[str, Any]] = [
            p for p in req_body.get("params", []) if p.get("name", "").strip()
        ]
        returns: str = req_body.get("returns", "str")
        fn_body: str | None = req_body.get("body") or None

        path = _find_agent_router_path()
        if not path:
            return JSONResponse({"ok": False, "error": "agent_router.py not found"})

        source = path.read_text()
        _m = _re.search(r"^(\w+)\s*=\s*Dependencies\.Client", source, _re.MULTILINE)
        ws_type = _m.group(1) if _m else "AppClient"

        fn_code = _build_tool_function(name, description, params, returns, ws_type, body=fn_body)
        updated = _splice_tool(source, fn_code, name)

        try:
            compile(updated, "agent_router.py", "exec")
        except SyntaxError as e:
            return JSONResponse({"ok": False, "error": f"Syntax error at line {e.lineno}: {e.msg}"})

        path.write_text(updated)
        return JSONResponse({"ok": True})

    @router.delete("/_apx/tools/{fn_name}", include_in_schema=False)
    async def delete_tool(fn_name: str) -> Any:
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
        return JSONResponse({"ok": True})

    @router.get("/_apx/deploy/stream", include_in_schema=False)
    async def stream_deploy(request: Request) -> Any:
        import asyncio as _asyncio
        import re as _re
        import shutil
        from fastapi.responses import StreamingResponse

        root = _find_deploy_root()
        _ANSI = _re.compile(r"\x1b\[[0-9;]*m")

        async def _generate():
            if root is None:
                yield "data: ERROR: could not find project root (pyproject.toml)\n\n"
                yield "data: __EXIT__1\n\n"
                return
            apx_bin = shutil.which("apx")
            if apx_bin is None:
                yield "data: ERROR: apx binary not found in PATH\n\n"
                yield "data: __EXIT__1\n\n"
                return
            yield f"data: Running: apx deploy {root}\n\n"
            try:
                proc = await _asyncio.create_subprocess_exec(
                    apx_bin, "deploy", str(root),
                    stdout=_asyncio.subprocess.PIPE,
                    stderr=_asyncio.subprocess.STDOUT,
                )
                assert proc.stdout is not None
                async for raw_line in proc.stdout:
                    line = _ANSI.sub("", raw_line.decode(errors="replace")).rstrip()
                    yield f"data: {line}\n\n"
                rc = await proc.wait()
                yield f"data: __EXIT__{rc}\n\n"
            except Exception as exc:
                yield f"data: __ERROR__{exc}\n\n"

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/_apx/setup", include_in_schema=False)
    async def setup_ui(request: Request) -> HTMLResponse:
        env_path = _find_env_path()
        current = _read_env_file(env_path) if env_path and env_path.exists() else {}
        return HTMLResponse(_render_setup_ui(current))

    @router.get("/_apx/setup/catalogs", include_in_schema=False)
    async def setup_catalogs(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        ws: WorkspaceClient = request.app.state.workspace_client
        try:
            cats = [c.name for c in ws.catalogs.list() if c.name]
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(sorted(cats))

    @router.get("/_apx/setup/schemas", include_in_schema=False)
    async def setup_schemas(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        catalog = request.query_params.get("catalog", "")
        if not catalog:
            return JSONResponse([])
        ws: WorkspaceClient = request.app.state.workspace_client
        try:
            schemas = [s.name for s in ws.schemas.list(catalog_name=catalog) if s.name
                       and s.name not in ("information_schema",)]
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(sorted(schemas))

    @router.get("/_apx/setup/warehouses", include_in_schema=False)
    async def setup_warehouses(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import asyncio as _asyncio
        ws: WorkspaceClient = request.app.state.workspace_client
        try:
            whs = await _asyncio.to_thread(lambda: [
                {"id": w.id, "name": w.name or w.id, "state": getattr(w.state, "value", str(w.state))}
                for w in ws.warehouses.list() if w.id
            ])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(whs)

    @router.post("/_apx/setup", include_in_schema=False)
    async def save_setup(request: Request) -> Any:
        from fastapi.responses import JSONResponse

        body = await request.json()
        catalog: str = body.get("catalog", "").strip()
        schema: str = body.get("schema", "").strip()
        wh_id: str = body.get("warehouse_id", "").strip()
        if not catalog or not schema or not wh_id:
            return JSONResponse({"ok": False, "error": "catalog, schema, and warehouse_id required"})

        env_path = _find_env_path()
        if env_path is None:
            return JSONResponse({"ok": False, "error": "Could not find project root"})

        _write_env_file(env_path, {
            "DEMO_CATALOG": catalog,
            "DEMO_SCHEMA": schema,
            "WAREHOUSE_ID": wh_id,
        })

        instructions: str | None = None
        if body.get("generate_instructions"):
            ctx: AgentContext | None = request.app.state.agent_context
            ws: WorkspaceClient = request.app.state.workspace_client
            instructions = await _generate_agent_instructions(ws, ctx, catalog, schema, wh_id)

        return JSONResponse({"ok": True, "instructions": instructions})

    @router.post("/_apx/setup/generate-instructions", include_in_schema=False)
    async def regen_instructions(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        body = await request.json()
        ctx: AgentContext | None = request.app.state.agent_context
        ws: WorkspaceClient = request.app.state.workspace_client
        instructions = await _generate_agent_instructions(
            ws, ctx, body.get("catalog", ""), body.get("schema", ""), body.get("warehouse_id", ""),
        )
        return JSONResponse({"ok": True, "instructions": instructions})

    @router.post("/_apx/setup/apply-instructions", include_in_schema=False)
    async def apply_instructions(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import re as _re

        body = await request.json()
        new_instructions: str = body.get("instructions", "").strip()
        if not new_instructions:
            return JSONResponse({"ok": False, "error": "No instructions provided"})

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx:
            addendum = ""
            if "[DEV MODE]" in (ctx.config.instructions or ""):
                addendum = "\n\n" + ctx.config.instructions.split("[DEV MODE]", 1)[1].strip()
                addendum = "\n\n[DEV MODE]" + addendum
            ctx.config.instructions = new_instructions + addendum

        root = _find_deploy_root()
        if root:
            toml_path = root / "pyproject.toml"
            if toml_path.exists():
                src = toml_path.read_text()
                escaped = new_instructions.replace('\\', '\\\\').replace('"""', '\\"\\"\\"')
                new_block = f'instructions = """\n{escaped}\n"""'
                updated = _re.sub(r'instructions\s*=\s*"""[\s\S]*?"""', new_block, src, count=1)
                if updated != src:
                    toml_path.write_text(updated)

        return JSONResponse({"ok": True})

    @router.get("/_apx/eval/data", include_in_schema=False)
    async def eval_data_get() -> Any:
        """Read persisted eval cases. Returns [] if no file or no agent_router."""
        from fastapi.responses import JSONResponse
        path = _find_evals_path()
        if path is None or not path.exists():
            return JSONResponse([])
        try:
            return JSONResponse(_json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/_apx/eval/data", include_in_schema=False)
    async def eval_data_post(request: Request) -> Any:
        """Replace persisted eval cases with the request body (a list)."""
        from fastapi.responses import JSONResponse
        body = await request.json()
        if not isinstance(body, list):
            return JSONResponse({"ok": False, "error": "Body must be a list"}, status_code=400)
        path = _find_evals_path()
        if path is None:
            return JSONResponse({"ok": False, "error": "agent_router.py not found in running process"}, status_code=503)
        try:
            path.write_text(_json.dumps(body, indent=2))
        except OSError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "count": len(body)})

    @router.post("/_apx/eval/judge", include_in_schema=False)
    async def eval_judge(request: Request) -> Any:
        """LLM-as-judge scoring for eval cases.

        Body: {question, response, criterion, model?}. The judge prompt asks the
        model to reply with PASS/FAIL + a one-sentence reason; we parse the
        verdict deterministically and return {ok, pass, verdict, reason}.
        """
        from fastapi.responses import JSONResponse
        import time as _time

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        body = await request.json()
        question = (body.get("question") or "").strip()
        response = (body.get("response") or "").strip()
        criterion = (body.get("criterion") or "").strip()
        if not (question and response and criterion):
            return JSONResponse(
                {"ok": False, "error": "question, response, and criterion are all required"},
                status_code=400,
            )
        model = body.get("model") or getattr(ctx.config, "model", "")
        if not model:
            return JSONResponse({"ok": False, "error": "No model configured"}, status_code=400)

        try:
            from databricks_openai import AsyncDatabricksOpenAI
        except ImportError as exc:
            return JSONResponse({"ok": False, "error": f"databricks_openai not available: {exc}"}, status_code=500)

        prompt = (
            "You are evaluating an AI agent's response against a criterion. "
            "Reply on a single line in this exact format:\n"
            "VERDICT: PASS|FAIL\n"
            "REASON: <one sentence>\n\n"
            f"Question: {question}\n"
            f"Response: {response}\n"
            f"Criterion: {criterion}\n"
            "Strict pass: response clearly meets the criterion. If unclear or partial, FAIL."
        )

        t0 = _time.monotonic()
        try:
            client = AsyncDatabricksOpenAI()
            resp = await client.responses.create(
                model=model,
                input=[{"role": "user", "content": prompt}],
            )
            elapsed = int((_time.monotonic() - t0) * 1000)
            text = (getattr(resp, "output_text", "") or "").strip()
            verdict, reason = _parse_judge_output(text)
            return JSONResponse({
                "ok": True,
                "pass": verdict == "PASS",
                "verdict": verdict,
                "reason": reason,
                "duration_ms": elapsed,
                "model": model,
            })
        except Exception as exc:  # noqa: BLE001
            elapsed = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({
                "ok": False,
                "error": str(exc),
                "duration_ms": elapsed,
            }, status_code=200)

    # Redirects for old routes
    @router.get("/_apx/eval", include_in_schema=False)
    async def eval_ui() -> Any:
        from starlette.responses import RedirectResponse as _R
        return _R("/_apx/agent", status_code=302)

    @router.get("/_apx/wizard", include_in_schema=False)
    async def wizard_ui() -> Any:
        from starlette.responses import RedirectResponse as _R
        return _R("/_apx/setup", status_code=302)

    @router.get("/_apx/wizard/tables", include_in_schema=False)
    async def wizard_tables(request: Request, catalog: str, schema: str) -> Any:
        from fastapi.responses import JSONResponse
        ws = WorkspaceClient()
        warehouse_id = os.environ.get("WAREHOUSE_ID", "")
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

    return router
