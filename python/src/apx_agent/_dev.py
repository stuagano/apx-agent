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
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ._models import AgentContext, AgentTool
from ._topology import build_topology, inspect_node
from ._ui_chat import (
    _render_agent_ui,
    _render_unified_shell,
    _build_apx_openapi_spec,
)
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
    _parse_agent_nodes,
)
from ._ui_setup import (
    _find_env_path,
    _read_env_file,
    _write_env_file,
    _render_setup_ui,
)
from ._ui_probe import _generate_agent_instructions, _render_probe_ui, _run_probe_checks, _discover_vs_indexes
from ._ui_nav import _apx_nav_css, _apx_nav_html, _deploy_overlay_html

logger = logging.getLogger(__name__)

_TRACE_CSS = """
  :root{--bg:#0a0a0a;--panel:#111;--border:#2a2a2a;--text:#e5e7eb;--muted:#888;
        --accent:#60b0ff;--accent-bg:#0d1f38;--accent-border:#1e3a5f;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;
       font-size:13px;background:var(--bg);color:var(--text);min-height:100vh;}
  header{height:48px;padding:0 16px;display:flex;align-items:center;gap:12px;
         background:var(--panel);border-bottom:1px solid var(--border);}
  .badge{background:var(--accent-bg);color:var(--accent);font-size:11px;
         font-weight:600;padding:2px 8px;border-radius:4px;letter-spacing:.5px;
         text-transform:uppercase;}
  h1{font-size:14px;font-weight:600;}
  .back{margin-left:auto;font-size:12px;color:var(--accent);text-decoration:none;
        padding:4px 10px;border:1px solid var(--accent-border);border-radius:5px;
        background:var(--accent-bg);}
  .back:hover{background:#112a4a;}
  main{padding:24px 28px;max-width:960px;}
  .meta{color:var(--muted);font-size:11px;margin-bottom:20px;
        font-family:monospace;word-break:break-all;}
  /* Trace list */
  table{width:100%;border-collapse:collapse;font-size:12px;}
  th{color:var(--muted);text-align:left;padding:6px 10px;
     border-bottom:1px solid var(--border);font-weight:500;white-space:nowrap;}
  td{padding:7px 10px;border-bottom:1px solid #1a1a1a;vertical-align:top;}
  tr:hover td{background:#111;}
  td a{color:var(--accent);text-decoration:none;}
  td a:hover{text-decoration:underline;}
  .st-ok{color:#4ade80;} .st-err{color:#f87171;} .st-run{color:#facc15;}
  .preview{max-width:360px;overflow:hidden;text-overflow:ellipsis;
           white-space:nowrap;color:var(--muted);}
  .dur{font-family:monospace;color:var(--muted);white-space:nowrap;}
  .empty{color:var(--muted);padding:32px 10px;font-style:italic;}
  /* Span tree */
  .span-tree{display:flex;flex-direction:column;gap:6px;}
  .span-card{background:var(--panel);border:1px solid var(--border);
             border-radius:7px;overflow:hidden;}
  .span-head{display:flex;align-items:center;gap:10px;padding:9px 13px;
             cursor:pointer;user-select:none;}
  .span-head:hover{background:#151515;}
  .stype{font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;
         text-transform:uppercase;letter-spacing:.4px;white-space:nowrap;}
  .stype-LLM{color:#22d3ee;background:#042929;}
  .stype-TOOL{color:#facc15;background:#1a1500;}
  .stype-CHAIN{color:#a78bfa;background:#1a1030;}
  .stype-AGENT{color:#60b0ff;background:#0d1f38;}
  .stype-OTHER{color:#94a3b8;background:#1a1a1a;}
  .sname{font-weight:500;font-size:13px;flex:1;overflow:hidden;
         text-overflow:ellipsis;white-space:nowrap;}
  .sdur{font-family:monospace;font-size:11px;color:var(--muted);white-space:nowrap;}
  .sstatus{font-size:10px;font-weight:600;white-space:nowrap;}
  .sstatus-ok{color:#4ade80;} .sstatus-err{color:#f87171;}
  .span-body{padding:10px 14px;border-top:1px solid var(--border);
             display:none;flex-direction:column;gap:8px;}
  .span-body.open{display:flex;}
  .io-block{display:flex;flex-direction:column;gap:4px;}
  .io-label{font-size:10px;text-transform:uppercase;letter-spacing:.5px;
            color:#555;font-weight:600;}
  pre.io-pre{background:#0d0d0d;border:1px solid #1e1e1e;border-radius:5px;
             padding:8px 10px;font-size:11px;font-family:monospace;
             color:#aaa;white-space:pre-wrap;word-break:break-all;
             max-height:240px;overflow-y:auto;}
  .indent{border-left:2px solid var(--border);padding-left:16px;margin-top:4px;}
  .err-banner{background:#2a0f0f;border:1px solid #7f1d1d;border-radius:6px;
              padding:12px 16px;color:#fda4af;margin-bottom:16px;}
"""


def _span_type_css(span_type: str) -> str:
    t = (span_type or "").upper()
    if t in ("LLM", "CHAT_MODEL", "EMBEDDING"):
        return "LLM"
    if t in ("TOOL", "RETRIEVER"):
        return "TOOL"
    if t in ("CHAIN",):
        return "CHAIN"
    if t in ("AGENT",):
        return "AGENT"
    return "OTHER"


def _render_traces_list(rows: list, agent_name: str | None) -> str:
    import json as _json, html as _html

    title = f"{agent_name} — traces" if agent_name else "Traces"
    if not rows:
        body = '<p class="empty">No traces found. Run the agent and traces will appear here.</p>'
    else:
        ths = "<tr><th>Time</th><th>Duration</th><th>Status</th><th>Request</th><th>Response</th></tr>"
        tds = []
        for r in rows:
            ts = r["request_time_ms"]
            import datetime
            dt = datetime.datetime.fromtimestamp(ts / 1000).strftime("%m/%d %H:%M:%S") if ts else "—"
            dur = f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "—"
            st = r["state"]
            st_cls = "st-ok" if "OK" in st or "COMPLETE" in st else ("st-err" if "ERR" in st or "FAIL" in st else "st-run")
            req = _html.escape((r["request_preview"] or "")[:120])
            resp = _html.escape((r["response_preview"] or "")[:120])
            tid = _html.escape(r["trace_id"])
            tds.append(
                f'<tr>'
                f'<td><a href="/_apx/traces/{tid}">{dt}</a></td>'
                f'<td class="dur">{dur}</td>'
                f'<td class="{st_cls}">{st}</td>'
                f'<td class="preview">{req}</td>'
                f'<td class="preview">{resp}</td>'
                f'</tr>'
            )
        body = f'<table>{ths}{"".join(tds)}</table>'

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{_html.escape(title)}</title>
<style>{_TRACE_CSS}</style></head><body>
<header>
  <span class="badge">APX</span><h1>{_html.escape(title)}</h1>
  <a class="back" href="/_apx/agent">← Agent</a>
</header>
<main>{body}</main>
</body></html>"""


def _render_trace_detail(trace_id: str, spans: list | None, error: str | None) -> str:
    import json as _json, html as _html

    err_html = f'<div class="err-banner">{_html.escape(error or "Unknown error")}</div>' if error else ""

    if not spans:
        body = err_html + '<p class="empty">No spans found for this trace.</p>'
    else:
        # Build parent→children map
        children: dict = {}
        roots = []
        for s in spans:
            pid = s.get("parent_id")
            if pid:
                children.setdefault(pid, []).append(s)
            else:
                roots.append(s)

        def _render_span(s: dict, depth: int = 0) -> str:
            st = _span_type_css(s.get("span_type", ""))
            dur = f"{s['duration_ms']}ms" if s.get("duration_ms") is not None else "—"
            status = s.get("status", "")
            st_cls = "sstatus-ok" if "OK" in status.upper() else "sstatus-err"
            name = _html.escape(s.get("name", ""))
            sid = _html.escape(s.get("span_id", ""))
            inp = _json.dumps(s.get("inputs"), indent=2) if s.get("inputs") else None
            out = _json.dumps(s.get("outputs"), indent=2) if s.get("outputs") else None
            io_html = ""
            if inp:
                io_html += f'<div class="io-block"><div class="io-label">Inputs</div><pre class="io-pre">{_html.escape(inp[:4000])}</pre></div>'
            if out:
                io_html += f'<div class="io-block"><div class="io-label">Outputs</div><pre class="io-pre">{_html.escape(out[:4000])}</pre></div>'
            kids = "".join(_render_span(c, depth + 1) for c in children.get(s.get("span_id", ""), []))
            indent = f'<div class="indent">{kids}</div>' if kids else ""
            return (
                f'<div class="span-card">'
                f'<div class="span-head" onclick="this.nextSibling.classList.toggle(\'open\')">'
                f'<span class="stype stype-{st}">{st}</span>'
                f'<span class="sname">{name}</span>'
                f'<span class="sdur">{dur}</span>'
                f'<span class="sstatus {st_cls}">{status}</span>'
                f'</div>'
                f'<div class="span-body">{io_html}</div>'
                f'</div>'
                f'{indent}'
            )

        tree_html = '<div class="span-tree">' + "".join(_render_span(s) for s in roots) + "</div>"
        body = err_html + tree_html

    tid_escaped = _html.escape(trace_id)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Trace {tid_escaped}</title>
<style>{_TRACE_CSS}</style></head><body>
<header>
  <span class="badge">APX</span><h1>Trace</h1>
  <a class="back" href="/_apx/traces">← All traces</a>
</header>
<main>
  <div class="meta">ID: {tid_escaped}</div>
  {body}
</main>
</body></html>"""


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
            "The tool is appended to agent.py; restart `apx run` (or redeploy) to load it."
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
        "The tool will be generated and inserted into agent_router.py; restart `apx run` to load it."
    )
    ctx.config.instructions = (ctx.config.instructions or "") + _dev_addendum
    logger.info("Dev mode: create_tool meta-tool injected into agent context")


def _persist_instructions(
    ctx: "AgentContext | None",
    instructions: str,
    ws_client: "WorkspaceClient | None" = None,
) -> None:
    """Write instructions into the root agent's ``Agent(instructions=...)`` in
    agent.py — the single source of truth the editor edits and the runtime
    imports — plus an in-memory update and best-effort workspace write-back.

    (Previously wrote pyproject.toml's ``[tool.apx_agent].instructions``, which
    the running ``LlmAgent`` ignores — so Setup's "apply" had no effect. Writing
    agent.py is what actually connects Setup, the editor, and the live agent.)
    """
    from ._ui_edit import _find_agent_router_path, _set_agent_instructions

    if ctx:
        addendum = ""
        if "[DEV MODE]" in (ctx.config.instructions or ""):
            dev_suffix = ctx.config.instructions.split("[DEV MODE]", 1)[1]
            addendum = "\n\n[DEV MODE]" + dev_suffix
        ctx.config.instructions = instructions + addendum

    path = _find_agent_router_path()
    if not path or not path.exists():
        return
    src = path.read_text()
    try:
        updated = _set_agent_instructions(src, instructions)
        compile(updated, str(path), "exec")  # never write syntactically broken source
    except Exception as exc:  # noqa: BLE001 — bad parse/splice: skip the write, don't corrupt
        logger.warning("Could not update instructions in %s: %s", path, exc)
        return
    if updated == src:
        return
    path.write_text(updated)
    if ws_client and ctx:
        app_name = ctx.config.name if ctx else None
        if app_name:
            try:
                app_info = ws_client.apps.get(app_name)
                ws_source = getattr(app_info, "default_source_code_path", None)
                if ws_source:
                    ws_path = ws_source.rstrip("/") + "/" + path.name
                    ws_client.workspace.upload(ws_path, updated.encode(), overwrite=True)
            except Exception:
                pass


async def _ws_upload_agent_file(request: Request, local_path: "Path", content: str) -> None:
    """Write-back helper: upload a local agent file to its workspace counterpart."""
    import asyncio as _asyncio
    ctx: "AgentContext | None" = getattr(request.app.state, "agent_context", None)
    ws_client: "WorkspaceClient | None" = getattr(request.app.state, "workspace_client", None)
    app_name = ctx.config.name if ctx else None
    if not app_name or not ws_client:
        return
    try:
        app_info = await _asyncio.to_thread(lambda: ws_client.apps.get(app_name))
        ws_source = getattr(app_info, "default_source_code_path", None)
        if ws_source:
            ws_path = ws_source.rstrip("/") + f"/{local_path.name}"
            await _asyncio.to_thread(
                lambda: ws_client.workspace.upload(ws_path, content.encode(), overwrite=True)
            )
    except Exception:
        pass


def build_dev_ui_router(api_prefix: str = "/api") -> APIRouter:
    """Build the /_apx/* dev UI routes."""
    from fastapi.responses import RedirectResponse

    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/_apx/agent")

    @router.get("/_apx/agent", include_in_schema=False)
    async def agent_dev_ui(request: Request) -> HTMLResponse:
        """Unified tabbed shell hosting every /_apx/* page in one iframe."""
        ctx: AgentContext | None = request.app.state.agent_context
        return HTMLResponse(_render_unified_shell(ctx))

    @router.get("/_apx/chat", include_in_schema=False)
    async def chat_dev_ui(request: Request) -> HTMLResponse:
        """Bare chat content — loaded by the unified shell's Chat tab.

        Older bookmarks of /_apx/agent (which used to point at the chat
        page) now land on the shell. The shell's default tab is Chat, so
        the user-visible behaviour is unchanged.
        """
        ctx: AgentContext | None = request.app.state.agent_context
        return HTMLResponse(_render_agent_ui(ctx))

    @router.get("/_apx/tools", include_in_schema=False)
    async def tools_dev_ui() -> Any:
        # The standalone tools page is retired — tool authoring (incl. the
        # natural-language generator) now lives in the Edit page's New Tool modal.
        from starlette.responses import RedirectResponse as _R
        return _R("/_apx/edit", status_code=302)

    @router.get("/_apx/openapi.json", include_in_schema=False)
    async def apx_openapi_spec(request: Request) -> Any:
        from fastapi.responses import JSONResponse
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
        return JSONResponse(
            _build_apx_openapi_spec(ctx, api_prefix, base_url=base_url)
        )

    @router.get("/_apx/probe", include_in_schema=False)
    async def probe_dev_ui() -> HTMLResponse:
        return HTMLResponse(_render_probe_ui())

    @router.get("/_apx/probe/checks", include_in_schema=False)
    async def probe_checks(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        ctx: AgentContext | None = request.app.state.agent_context
        session_store = getattr(request.app.state, "session_store", None)
        return JSONResponse(
            await _run_probe_checks(
                ctx,
                headers=dict(request.headers),
                session_store=session_store,
            )
        )

    @router.get("/_apx/traces", include_in_schema=False)
    async def traces_list_ui(request: Request) -> Any:
        import os
        from fastapi.responses import JSONResponse
        ctx: AgentContext | None = getattr(request.app.state, "agent_context", None)
        agent_name = ctx.config.name if ctx else None
        fmt = request.query_params.get("fmt")
        max_results = int(request.query_params.get("max", "50"))
        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID")
        try:
            import mlflow as _mlflow
            # include_spans=False skips artifact download — works even when
            # the blob-storage endpoint is unreachable (e.g. private-link
            # workspaces where *.storage.cloud.databricks.com is blocked).
            from mlflow.tracking import MlflowClient as _MlflowClient
            client = _MlflowClient()
            # MLflow's search_traces(experiment_ids=None) trips on the local
            # sqlite store ("'NoneType' object is not iterable"), which is the
            # default backend for local `apx run` — so the Trace panel sees
            # nothing even when traces are being recorded. Resolve to all
            # experiments when no MLFLOW_EXPERIMENT_ID is set so the dev loop
            # surfaces its traces. In the deployed runtime MLFLOW_EXPERIMENT_ID
            # is always set and this branch is a no-op.
            exp_ids = (
                [experiment_id] if experiment_id
                else [e.experiment_id for e in client.search_experiments()]
            )
            traces = list(client.search_traces(
                experiment_ids=exp_ids,
                max_results=max_results,
                order_by=["timestamp DESC"],
                include_spans=False,
            )) if exp_ids else []
        except Exception:
            traces = []
        rows = []
        for t in traces:
            info = t.info
            dur_ms = int(info.execution_duration / 1_000_000) if info.execution_duration else None
            rows.append({
                "trace_id": info.trace_id,
                "state": info.state.value if hasattr(info.state, "value") else str(info.state),
                "request_time_ms": info.request_time,
                "duration_ms": dur_ms,
                "request_preview": info.request_preview or "",
                "response_preview": info.response_preview or "",
            })
        if fmt == "json":
            return JSONResponse(rows)
        return HTMLResponse(_render_traces_list(rows, agent_name))

    @router.get("/_apx/traces/{trace_id:path}", include_in_schema=False)
    async def trace_detail_ui(trace_id: str, request: Request) -> Any:
        from fastapi.responses import JSONResponse
        fmt = request.query_params.get("fmt")
        try:
            import mlflow as _mlflow
            trace = _mlflow.get_trace(trace_id)
        except Exception as exc:
            if fmt == "json":
                return JSONResponse({"error": str(exc)}, status_code=404)
            return HTMLResponse(_render_trace_detail(trace_id, None, str(exc)))
        if trace is None:
            if fmt == "json":
                return JSONResponse({"error": "not found"}, status_code=404)
            return HTMLResponse(_render_trace_detail(trace_id, None, "Trace not found"))
        spans = list(getattr(trace.data, "spans", None) or [])
        span_dicts = []
        for s in spans:
            span_dicts.append({
                "span_id": s.span_id,
                "parent_id": s.parent_id,
                "name": s.name,
                "span_type": s.span_type.value if hasattr(s.span_type, "value") else str(s.span_type),
                "status": s.status.status_code.value if hasattr(getattr(s.status, "status_code", None), "value") else str(s.status),
                "start_time_ns": s.start_time_ns,
                "end_time_ns": s.end_time_ns,
                "duration_ms": round((s.end_time_ns - s.start_time_ns) / 1_000_000, 1) if s.end_time_ns and s.start_time_ns else None,
                "inputs": s.inputs,
                "outputs": s.outputs,
            })
        if fmt == "json":
            return JSONResponse({"trace_id": trace_id, "spans": span_dicts})
        return HTMLResponse(_render_trace_detail(trace_id, span_dicts, None))

    # ------------------------------------------------------------------
    # Topology UI — interactive react-flow graph at /_apx/topology.
    # Serves the built React bundle from _static/topology/, plus two JSON
    # endpoints the UI fetches: /_apx/topology.json (full graph) and
    # /_apx/topology/inspect/{node_id} (per-node details).
    # ------------------------------------------------------------------

    from pathlib import Path as _TopoPath
    _topo_static_root = _TopoPath(__file__).parent / "_static" / "topology"

    @router.get("/_apx/topology", include_in_schema=False)
    async def topology_index() -> Any:
        index = _topo_static_root / "index.html"
        if not index.exists():
            return HTMLResponse(
                "Topology UI not built — run "
                "<code>cd python/dev-ui/topology &amp;&amp; npm run build</code>.",
                status_code=503,
            )
        return FileResponse(index, media_type="text/html")

    @router.get("/_apx/topology.json", include_in_schema=False)
    async def topology_json(request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse(
                {"error": "Agent context not available"}, status_code=503
            )
        return JSONResponse(build_topology(ctx))

    @router.get("/_apx/topology/inspect/{node_id:path}", include_in_schema=False)
    async def topology_inspect(node_id: str, request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse(
                {"error": "Agent context not available"}, status_code=503
            )
        details = inspect_node(ctx, node_id)
        if details is None:
            raise HTTPException(status_code=404, detail=f"Node not found: {node_id}")
        return JSONResponse(details)

    @router.get("/_apx/topology/assets/{path:path}", include_in_schema=False)
    async def topology_assets(path: str) -> Any:
        target = _topo_static_root / "assets" / path
        if target.is_file():
            return FileResponse(target)
        raise HTTPException(status_code=404, detail="asset not found")

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
            resp = await client.chat.completions.create(model=model, messages=messages)
            elapsed = int((_time.monotonic() - t0) * 1000)
            choices = getattr(resp, "choices", None) or []
            output = ""
            if choices:
                msg = getattr(choices[0], "message", None)
                output = (getattr(msg, "content", None) or "") if msg else ""
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
        import asyncio as _asyncio
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

        # Write back to workspace so the change survives restarts.
        ctx: AgentContext | None = request.app.state.agent_context
        ws_client: WorkspaceClient | None = getattr(request.app.state, "workspace_client", None)
        app_name = ctx.config.name if ctx else None
        restart_required = False
        if app_name and ws_client:
            try:
                app_info = await _asyncio.to_thread(lambda: ws_client.apps.get(app_name))
                ws_source = getattr(app_info, "default_source_code_path", None)
                if ws_source:
                    ws_path = ws_source.rstrip("/") + f"/{path.name}"
                    await _asyncio.to_thread(
                        lambda: ws_client.workspace.upload(ws_path, content.encode(), overwrite=True)
                    )
                    restart_required = True
            except Exception:
                pass

        return JSONResponse({"ok": True, "restart_required": restart_required})

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
        target: str = (req_body.get("agent") or "agent").strip() or "agent"

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
        from ._ui_edit import _parse_agent_nodes
        nodes = _parse_agent_nodes(updated)
        wired = any(name in (n.get("tools") or []) for n in nodes)
        if not wired:
            leaves = [n["name"] for n in nodes if n["name"] != "agent" and n.get("wrapper") is None]
            return JSONResponse({
                "ok": True, "wired": False, "agents": leaves,
                "note": (
                    f"`{name}` was added to agent.py but not attached to an agent "
                    f"(this agent is composed). Re-add it choosing one of: {', '.join(leaves) or '(define a leaf agent first)'}."
                ),
            })
        return JSONResponse({"ok": True, "wired": True})

    @router.delete("/_apx/tools/{fn_name}", include_in_schema=False)
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
        embed = request.query_params.get("embed") == "1"
        return HTMLResponse(_render_setup_ui(current, embed=embed))

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

    @router.get("/_apx/setup/agents", include_in_schema=False)
    async def setup_agents(request: Request) -> Any:
        """Return the agents defined in the LOCAL agent.py — the file the editor
        edits and the runtime imports — as ``[{name, tools, instructions, wrapper}]``.

        (Previously this discovered deployed *workspace* apps, which is the wrong
        data for the composer: it edits this project's agents, not other apps.)
        """
        from fastapi.responses import JSONResponse
        path = _find_agent_router_path()
        if not path or not path.exists():
            return JSONResponse([])
        return JSONResponse(_parse_agent_nodes(path.read_text()))

    @router.post("/_apx/setup", include_in_schema=False)
    async def save_setup(request: Request) -> Any:
        import asyncio as _asyncio
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
        if body.get("generate_instructions"):
            ctx: AgentContext | None = request.app.state.agent_context
            ws: WorkspaceClient = request.app.state.workspace_client
            instructions = await _generate_agent_instructions(ws, ctx, catalog, schema, wh_id)
            _persist_instructions(ctx, instructions, ws_client=ws)

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
        _persist_instructions(ctx, instructions, ws_client=ws)
        return JSONResponse({"ok": True, "instructions": instructions})

    @router.post("/_apx/setup/apply-instructions", include_in_schema=False)
    async def apply_instructions(request: Request) -> Any:
        from fastapi.responses import JSONResponse

        body = await request.json()
        new_instructions: str = body.get("instructions", "").strip()
        if not new_instructions:
            return JSONResponse({"ok": False, "error": "No instructions provided"})

        ctx: AgentContext | None = request.app.state.agent_context
        ws_client: WorkspaceClient | None = getattr(request.app.state, "workspace_client", None)
        _persist_instructions(ctx, new_instructions, ws_client=ws_client)
        return JSONResponse({"ok": True})

    @router.get("/_apx/setup/tools", include_in_schema=False)
    async def setup_list_tools() -> Any:
        from fastapi.responses import JSONResponse
        path = _find_agent_router_path()
        if not path or not path.exists():
            return JSONResponse([])
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
        return JSONResponse(result)

    @router.post("/_apx/setup/create-tool", include_in_schema=False)
    async def setup_create_tool(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import httpx

        body = await request.json()
        desc: str = body.get("description", "").strip()
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

        return JSONResponse({"ok": True, "tool_name": spec.get("name", "")})

    @router.post("/_apx/wizard/generate-tools", include_in_schema=False)
    async def wizard_generate_tools(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import httpx

        body = await request.json()
        description: str = body.get("description", "").strip()
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

        return JSONResponse({"ok": True, "tool_name": spec.get("name", "")})

    @router.post("/_apx/setup/wire-agent", include_in_schema=False)
    async def setup_wire_agent(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import json as _json

        body = await request.json()
        behavior: str = body.get("behavior", "").strip()
        agent_name: str = body.get("agent_name", "agent").strip()
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
            return JSONResponse({"ok": True, "tools": [], "instructions": f"You are {agent_name}. {behavior}"})

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
            return JSONResponse({"ok": True, "tools": selected, "instructions": instructions})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)})

    @router.post("/_apx/setup/agents", include_in_schema=False)
    async def setup_save_agents(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        import re as _re

        from ._ui_edit import _parse_agent_nodes, _set_agent_instructions, _set_agent_tools

        body = await request.json()
        nodes_data = body.get("nodes", [])
        if not isinstance(nodes_data, list):
            return JSONResponse({"ok": False, "error": "nodes must be a list"}, status_code=400)

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

        resp: dict[str, Any] = {"ok": True, "applied": applied}
        if skipped:
            resp["skipped"] = skipped
            resp["note"] = (
                "Editing existing agents is live; creating + wiring new agents "
                "(" + ", ".join(skipped) + ") is coming in the multi-agent step."
            )
        return JSONResponse(resp)

    @router.get("/_apx/setup/probe-json", include_in_schema=False)
    async def setup_probe_json(request: Request) -> Any:
        import time as _time
        from fastapi.responses import JSONResponse
        import httpx

        url = request.query_params.get("url", "").strip()
        if not url:
            return JSONResponse({"error": "url parameter required"}, status_code=400)

        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                r = await client.get(url)
            latency = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({"status": r.status_code, "latency_ms": latency, "url": url})
        except Exception as exc:
            latency = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({"error": str(exc), "latency_ms": latency, "url": url})

    @router.get("/_apx/setup/vs-indexes", include_in_schema=False)
    async def setup_vs_indexes(request: Request) -> Any:
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse
        ws: WorkspaceClient = request.app.state.workspace_client
        try:
            indexes = await _asyncio.to_thread(lambda: _discover_vs_indexes(ws))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse(indexes)

    @router.get("/_apx/setup/agent-pattern", include_in_schema=False)
    async def setup_get_agent_pattern() -> Any:
        from fastapi.responses import JSONResponse
        path = _find_agent_router_path()
        if not path or not path.exists():
            return JSONResponse({"type": "Agent"})
        nodes = _parse_agent_nodes(path.read_text())
        agent_node = next((n for n in nodes if n["name"] == "agent"), None)
        if not agent_node:
            return JSONResponse({"type": "Agent"})
        return JSONResponse({"type": agent_node["wrapper"] or "Agent"})

    @router.post("/_apx/setup/agent-pattern", include_in_schema=False)
    async def setup_set_agent_pattern(request: Request) -> Any:
        from fastapi.responses import JSONResponse

        body = await request.json()
        pattern: str = body.get("pattern", "").strip()

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
            return JSONResponse({"ok": True, "snippet": _SNIPPET_PATTERNS[pattern]})

        _AUTO_PATTERNS = {"Agent", "LlmAgent", "LoopAgent"}
        if pattern not in _AUTO_PATTERNS:
            return JSONResponse({"ok": False, "error": f"Unknown pattern: {pattern}"}, status_code=400)

        from ._ui_edit import _parse_agent_nodes, _set_agent_wrapper

        path = _find_agent_router_path()
        if not path or not path.exists():
            return JSONResponse({"ok": False, "error": "agent.py not found"}, status_code=404)

        source = path.read_text()
        nodes = _parse_agent_nodes(source)
        agent_node = next((n for n in nodes if n["name"] == "agent"), None)
        if not agent_node:
            return JSONResponse({"ok": False, "error": "No 'agent' variable in agent.py"}, status_code=400)

        current_type = agent_node["wrapper"] or "Agent"
        if current_type == pattern or (pattern in ("Agent", "LlmAgent") and current_type in ("Agent", "LlmAgent")):
            return JSONResponse({"ok": True, "type": current_type, "changed": False})

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
        return JSONResponse({"ok": True, "type": pattern, "changed": updated != source})

    @router.post("/_apx/setup/compose", include_in_schema=False)
    async def setup_compose(request: Request) -> Any:
        """Compose ≥2 leaf agents into a workflow root via the chosen pattern.

        Body: ``{pattern, nodes: [{name, tools, instructions, route_key?,
        route_description?}], start?}``. Writes the leaf agents + the workflow
        root into agent.py and adds the wrapper import.
        """
        from fastapi.responses import JSONResponse
        from ._ui_edit import _compose_agents

        body = await request.json()
        pattern: str = body.get("pattern", "").strip()
        nodes = body.get("nodes", [])
        start: str | None = body.get("start") or None
        if not isinstance(nodes, list):
            return JSONResponse({"ok": False, "error": "nodes must be a list"}, status_code=400)

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
        return JSONResponse({
            "ok": True, "type": pattern,
            "agents": [leaf["name"] for leaf in leaves],
            "changed": updated != source,
        })

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
        content = _json.dumps(body, indent=2)
        try:
            path.write_text(content)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        await _ws_upload_agent_file(request, path, content)
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
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
            elapsed = int((_time.monotonic() - t0) * 1000)
            choices = getattr(resp, "choices", None) or []
            text = ""
            if choices:
                msg = getattr(choices[0], "message", None)
                text = ((getattr(msg, "content", None) or "") if msg else "").strip()
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
    async def eval_ui() -> HTMLResponse:
        """Eval landing page — lists persisted eval cases.

        Running cases live in the Chat panel's right-side sub-tab. This
        standalone page surfaces the same ``evals.json`` data as a
        read-only list with a link back to Chat, so the Eval tab in the
        unified shell shows something useful instead of bouncing through
        a redirect.
        """
        from ._ui_chat import _render_eval_landing
        path = _find_evals_path()
        cases: list[dict[str, Any]] = []
        loaded_path: str | None = None
        load_error: str | None = None
        if path is not None and path.exists():
            loaded_path = str(path)
            try:
                cases = _json.loads(path.read_text())
                if not isinstance(cases, list):
                    cases = []
                    load_error = f"{path} did not contain a JSON list."
            except (OSError, ValueError) as exc:
                load_error = f"Could not parse {path}: {exc}"
        return HTMLResponse(_render_eval_landing(cases, loaded_path, load_error))

    @router.get("/_apx/wizard", include_in_schema=False)
    async def wizard_ui() -> Any:
        from starlette.responses import RedirectResponse as _R
        return _R("/_apx/setup", status_code=302)

    @router.get("/_apx/wizard/tables", include_in_schema=False)
    async def wizard_tables(request: Request, catalog: str, schema: str) -> Any:
        from fastapi.responses import JSONResponse
        ws: WorkspaceClient = request.app.state.workspace_client
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
