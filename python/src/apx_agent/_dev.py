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

import asyncio
import json as _json
import logging
import os
from pathlib import Path
from typing import Any, NamedTuple

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from ._ui_table import TABLE_CSS
from ._ui_theme import apx_theme_style

from ._apx_models import (
    AgentNodeInfo,
    AgentPatternResponse,
    ApplyInstructionsRequest,
    ApprovalActionResponse,
    ApprovalInfo,
    ColumnDescriptionsRequest,
    ColumnDescriptionsSaveResponse,
    ColumnSuggestRequest,
    ColumnSuggestResponse,
    ComposeRequest,
    EditPreviewRequest,
    EditSaveRequest,
    EditSaveResponse,
    EvalCaseIn,
    EvalCaseResponse,
    EvalDataSaveResponse,
    GenerateInstructionsRequest,
    GroundingColumnsResponse,
    GroundingGenerateRequest,
    GroundingGenerateResponse,
    JudgeRequest,
    JudgeResponse,
    ProbeResult,
    ReplayLlmRequest,
    ReplayToolRequest,
    SaveAgentsRequest,
    SetAgentPatternRequest,
    SetupInstructionsResponse,
    SetupSaveRequest,
    ToolDeleteResponse,
    ToolFromDescriptionRequest,
    ToolFromDescriptionResponse,
    ToolInfo,
    ToolNewRequest,
    ToolSchemaResponse,
    ToolSuggestRequest,
    ToolSuggestResponse,
    TopologyResponse,
    TopologyTracingResponse,
    TopologyTracingSetRequest,
    TopologyTracingSetResponse,
    TopologyDigestResponse,
    LastRouteResponse,
    LatencyTrendResponse,
    ToolFailRateResponse,
    TraceDiffResponse,
    TraceDetailResponse,
    TraceRow,
    VsIndexInfo,
    WarehouseInfo,
    WireAgentRequest,
    WireAgentResponse,
    WorkspaceContextResponse,
    DiscoverGraphResponse,
    DiscoverImpactResponse,
)
from ._cost import usd_for_token_usage
from ._models import AgentContext, AgentTool
from ._topology import (
    annotate_topology,
    build_topology,
    inspect_node,
    route_highlight_from_spans,
    summarize_topology,
)
from ._ui_chat import (
    _render_agent_ui,
    _render_unified_shell,
    _build_apx_openapi_spec,
    standalone_chat_redirect,
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
from ._ui_tools import (
    ToolsSaveRequest,
    _render_tools_ui,
    save_tool_source,
    tool_file_response,
    tools_payload as _tools_payload,
)
from ._ui_probe import _generate_agent_instructions, _render_probe_ui, _run_probe_checks, _discover_vs_indexes, _validate_probe_url, validate_wire_peer_url

logger = logging.getLogger(__name__)

# ── Stale-while-revalidate TTL cache ─────────────────────────────────────────
import time as _time_mod
from dataclasses import dataclass, field as _dc_field


@dataclass
class _TTLCache:
    """Thread-safe stale-while-revalidate cache for slow async fetches."""
    ttl: float  # seconds
    _value: Any = _dc_field(default=None, init=False)
    _expires: float = _dc_field(default=0.0, init=False)
    _refreshing: bool = _dc_field(default=False, init=False)

    @property
    def fresh(self) -> bool:
        return self._value is not None and _time_mod.monotonic() < self._expires

    def get(self) -> Any:
        return self._value

    def put(self, value: Any) -> None:
        self._value = value
        self._expires = _time_mod.monotonic() + self.ttl
        self._refreshing = False

    def clear(self) -> None:
        self._value = None
        self._expires = 0.0
        self._refreshing = False


# Module-level singletons — survive request boundaries, reset on process restart.
_EVAL_CASES_CACHE: _TTLCache = _TTLCache(ttl=300)   # 5 min — ratings change infrequently
_TRACES_LIST_CACHE: _TTLCache = _TTLCache(ttl=60)   # 60 s — traces change with each run
_GROUNDING_COLUMNS_CACHE: _TTLCache = _TTLCache(ttl=600)  # 10 min — UC comments rarely change


class _EditPanelInventory(NamedTuple):
    schemas: list[dict[str, Any]]
    agents: list[dict[str, Any]]


def _edit_panel_inventory(ctx: AgentContext | None) -> _EditPanelInventory:
    """Return live local-tool schemas and remote-agent descriptors for Edit."""
    if ctx is None:
        return _EditPanelInventory([], [])
    schemas: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    for tool in ctx.tools or []:
        item = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema or {},
        }
        if tool.sub_agent_url:
            agents.append({**item, "url": tool.sub_agent_url})
        else:
            schemas.append(item)
    return _EditPanelInventory(schemas, agents)


# ── Response contracts for the read-only MEMORY + CONVERSATIONS routes ───────
# These model the *existing* success shapes the dev UI's embedded JS already
# reads — field names/types mirror the source dataclasses exactly so the
# contract documents reality rather than reshaping it. Setting these as
# ``response_model`` (and flipping ``include_in_schema=True``) puts the three
# read-only GET routes into the native FastAPI OpenAPI schema. Error/empty
# paths are preserved: the 503 paths return an explicit ``JSONResponse`` so
# they bypass ``response_model`` validation.


class ConversationSummaryResponse(BaseModel):
    """One row from ``GET /_apx/conversations``.

    Mirrors :class:`apx_agent._conversation.Conversation`; the dev UI reads
    ``c.id`` / ``c.title`` / ``c.updated_at`` (``_ui_chat.py:1307``).
    ``title`` is nullable on the source (unset until synthesized).
    """

    id: str
    title: str | None = None
    created_at: int
    updated_at: int


class ConversationItemResponse(BaseModel):
    """One row from ``GET /_apx/conversations/{conv_id}/items``.

    The dev UI reads ``item.type`` and ``item.data.role`` /
    ``item.data.content`` (``_ui_chat.py:1387``, ``:1411``). ``data`` stays a
    loose mapping: the per-item payload is one of several ``ItemData`` shapes
    (message, function_call, …) the JS reads dynamically — a nested model
    would strip fields.
    """

    id: str
    type: str
    data: dict[str, Any]


class MemoryResponse(BaseModel):
    """One row from ``GET /_apx/memories``.

    Mirrors :class:`apx_agent._memory.Memory`; the landing-page preview reads
    ``m.content`` / ``m.updated_at`` (``_ui_chat.py:1667``). ``namespace`` is
    nullable to match callers that don't scope by namespace.
    """

    id: str
    content: str
    namespace: str | None = None
    updated_at: str


class _JudgeOutput(NamedTuple):
    verdict: str
    reason: str


_TRACE_CSS = """
  :root{--bg:var(--apx-bg);--panel:var(--apx-panel);--border:var(--apx-border);--text:var(--apx-text);--muted:var(--apx-muted);
        --accent:var(--apx-accent);--accent-bg:var(--apx-panel-2);--accent-border:var(--apx-border);}
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
  .back:hover{background:var(--apx-panel-2);}
  main{padding:24px 28px;max-width:960px;}
  .meta{color:var(--muted);font-size:11px;margin-bottom:20px;
        font-family:monospace;word-break:break-all;}
  /* Trace list */
  table{width:100%;border-collapse:collapse;font-size:12px;}
  th{color:var(--muted);text-align:left;padding:6px 10px;
     border-bottom:1px solid var(--border);font-weight:500;white-space:nowrap;}
  td{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top;}
  tr:hover td{background:var(--apx-panel);}
  td a{color:var(--accent);text-decoration:none;}
  td a:hover{text-decoration:underline;}
  .st-ok{color:var(--apx-ok);} .st-err{color:var(--apx-err);} .st-run{color:var(--apx-warn);}
  .tool-fails-card{display:flex;flex-wrap:wrap;gap:8px 14px;padding:10px 12px;
                   margin:0 0 12px;background:var(--apx-panel-2);border:1px solid var(--apx-panel-2);
                   border-radius:10px;font-size:12px;}
  #tool-fails[hidden]{display:none;}
  .tool-fail{font-family:monospace;}
  .trace-diff-bar{display:flex;align-items:center;gap:10px;margin:0 0 12px;
                  font-size:12px;color:var(--muted);}
  .trace-diff-go{background:var(--accent-bg);border:1px solid var(--accent-border);color:var(--accent);
                 border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;}
  .trace-diff-go:disabled{opacity:.4;cursor:not-allowed;}
  .td-pick{margin-right:6px;}
  .trace-diff-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
  .trace-diff-card{background:var(--apx-panel-2);border:1px solid var(--apx-panel-2);border-radius:10px;
                   padding:12px 14px;display:flex;flex-direction:column;gap:8px;}
  .trace-diff-label{font-size:11px;color:var(--muted);text-transform:uppercase;
                    letter-spacing:.04em;}
  .trace-diff-body{white-space:pre-wrap;font-size:13px;line-height:1.4;}
  .trace-diff-banner{font-size:12px;color:var(--muted);margin:0 0 12px;}
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
  .span-head:hover{background:var(--apx-panel-2);}
  .stype{font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;
         text-transform:uppercase;letter-spacing:.4px;white-space:nowrap;}
  .stype-LLM{color:var(--apx-cyan);background:var(--apx-cyan-soft);}
  .stype-TOOL{color:var(--apx-warn);background:var(--apx-warn-soft);}
  .stype-CHAIN{color:var(--apx-purple);background:var(--apx-purple-soft);}
  .stype-AGENT{color:var(--apx-accent-hover);background:var(--apx-accent-soft);}
  .stype-OTHER{color:var(--apx-muted);background:var(--apx-panel-2);}
  .sname{font-weight:500;font-size:13px;flex:1;overflow:hidden;
         text-overflow:ellipsis;white-space:nowrap;}
  .sdur{font-family:monospace;font-size:11px;color:var(--muted);white-space:nowrap;}
  .sstatus{font-size:10px;font-weight:600;white-space:nowrap;}
  .sstatus-ok{color:var(--apx-ok);} .sstatus-err{color:var(--apx-err);}
  .span-body{padding:10px 14px;border-top:1px solid var(--border);
             display:none;flex-direction:column;gap:8px;}
  .span-body.open{display:flex;}
  .io-block{display:flex;flex-direction:column;gap:4px;}
  .io-label{font-size:10px;text-transform:uppercase;letter-spacing:.5px;
            color:var(--apx-muted);font-weight:600;}
  pre.io-pre{background:var(--apx-bg);border:1px solid var(--apx-border);border-radius:5px;
             padding:8px 10px;font-size:11px;font-family:monospace;
             color:var(--apx-muted);white-space:pre-wrap;word-break:break-all;
             max-height:240px;overflow-y:auto;}
  /* Compact, deduped conversation view (chat payloads) */
  .convo{display:flex;flex-direction:column;gap:5px;}
  .tmsg{display:flex;gap:8px;font-size:12px;line-height:1.45;align-items:baseline;}
  .trole{flex:none;min-width:74px;color:var(--apx-muted);font-size:10px;font-weight:600;
         text-transform:uppercase;letter-spacing:.4px;font-family:ui-monospace,monospace;}
  .trole-tool{color:var(--apx-ok);}
  .tcontent{color:var(--apx-text);white-space:pre-wrap;word-break:break-word;
            font-family:ui-monospace,monospace;font-size:11.5px;}
  details.tsys{font-size:12px;}
  details.tsys>summary,details.tprefix>summary{cursor:pointer;color:var(--apx-muted);font-size:11px;
            list-style:none;padding:1px 0;}
  details.tsys>summary::-webkit-details-marker,
  details.tprefix>summary::-webkit-details-marker{display:none;}
  details.tsys>summary::before,details.tprefix>summary::before{content:"▸ ";color:var(--apx-border);}
  details.tsys[open]>summary::before,details.tprefix[open]>summary::before{content:"▾ ";}
  details.tprefix{border-left:2px solid var(--apx-border);padding-left:8px;margin:2px 0;}
  details.tprefix[open]{display:flex;flex-direction:column;gap:5px;}
  pre.tpre{background:var(--apx-bg);border:1px solid var(--apx-border);border-radius:5px;margin:4px 0 0;
           padding:8px 10px;font-size:11px;font-family:monospace;color:var(--apx-muted);
           white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto;}
  .span-event{font-size:11px;color:var(--apx-muted);padding:3px 14px 3px 30px;
              border-top:1px solid var(--apx-panel-2);font-family:monospace;
              white-space:pre-wrap;word-break:break-word;}
  .indent{border-left:2px solid var(--border);padding-left:16px;margin-top:4px;}
  .err-banner{background:var(--apx-err-soft);border:1px solid var(--apx-err);border-radius:6px;
              padding:12px 16px;color:var(--apx-err);margin-bottom:16px;}
"""
_TRACE_CSS += "\n" + TABLE_CSS



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


def _render_traces_list(rows: list | None, agent_name: str | None) -> str:
    import html as _html

    title = f"{agent_name} — traces" if agent_name else "Traces"
    # rows=None → skeleton mode: JS fetches ?fmt=json and renders client-side
    if rows is None:
        body = '<div id="traces-body"><p class="empty">Loading traces…</p></div>'
    elif not rows:
        body = '<div id="traces-body"><p class="empty">No traces found. Run the agent and traces will appear here.</p></div>'
    else:
        ths = "<tr><th>Time</th><th>Duration</th><th>Cost</th><th>Status</th><th>Request</th><th>Response</th><th>Feedback</th></tr>"
        tds = []
        for r in rows:
            ts = r["request_time_ms"]
            import datetime
            dt = datetime.datetime.fromtimestamp(ts / 1000).strftime("%m/%d %H:%M:%S") if ts else "—"
            dur = f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "—"
            cost = r.get("cost_usd")
            cost_str = f"${cost * 100:.3f}¢" if cost is not None and cost > 0 else "—"
            st = r["state"]
            st_cls = "st-ok" if "OK" in st or "COMPLETE" in st else ("st-err" if "ERR" in st or "FAIL" in st else "st-run")
            req = _html.escape((r["request_preview"] or "")[:120])
            resp = _html.escape((r["response_preview"] or "")[:120])
            tid = _html.escape(r["trace_id"])
            tid_js = tid.replace("'", "\\'")
            tds.append(
                f'<tr>'
                f'<td><a href="/_apx/traces/{tid}">{dt}</a></td>'
                f'<td class="dur">{dur}</td>'
                f'<td class="dur" title="input+output tokens cost (claude-sonnet-4-6)">{cost_str}</td>'
                f'<td class="{st_cls}">{st}</td>'
                f'<td class="preview">{req}</td>'
                f'<td class="preview">{resp}</td>'
                f'<td class="fb-cell">'
                f'<input type="checkbox" class="td-pick" data-tid="{tid}" />'
                f'<button class="fb-btn up" onclick="submitFeedback(\'{tid_js}\',true,this)">👍</button>'
                f'<button class="fb-btn down" onclick="submitFeedback(\'{tid_js}\',false,this)">👎</button>'
                f'</td>'
                f'</tr>'
            )
        body = f'<div id="traces-body"><table>{ths}{"".join(tds)}</table></div>'

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{_html.escape(title)}</title>
{apx_theme_style()}
<style>{_TRACE_CSS}
.fb-cell{{white-space:nowrap}}
.fb-btn{{background:none;border:1px solid var(--border,var(--apx-border));border-radius:4px;cursor:pointer;font-size:.9rem;padding:.1rem .4rem;opacity:.5}}
.fb-btn:hover{{opacity:1}}
.fb-btn.active.up{{opacity:1;border-color:var(--apx-ok)}}
.fb-btn.active.down{{opacity:1;border-color:var(--apx-err)}}
</style>
<script>
async function submitFeedback(tid, value, btn) {{
  try {{
    const r = await fetch('/_apx/feedback', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{trace_id: tid, name: 'quality', value, idempotency_key: tid}})
    }});
    if (!r.ok) throw new Error(await r.text());
    const row = btn.closest('tr');
    row.querySelectorAll('.fb-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    fetch('/_apx/eval/cache/bust', {{method:'POST'}}).catch(()=>{{}});
  }} catch(e) {{ alert('Feedback failed: ' + e.message); }}
}}

function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}

function renderRows(rows) {{
  const wrap = document.getElementById('traces-body');
  if (!rows.length) {{
    wrap.innerHTML = '<p class="empty">No traces found. Run the agent and traces will appear here.</p>';
    return;
  }}
  const ths = '<tr><th>Time</th><th>Duration</th><th>Cost</th><th>Status</th><th>Request</th><th>Response</th><th>Feedback</th></tr>';
  const tds = rows.map(r => {{
    const dt = r.request_time_ms
      ? new Date(r.request_time_ms).toLocaleString('en-US', {{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}})
      : '—';
    const dur = r.duration_ms != null ? r.duration_ms + 'ms' : '—';
    const cost = r.cost_usd != null && r.cost_usd > 0 ? (r.cost_usd * 100).toFixed(3) + '¢' : '—';
    const st = r.state || '';
    const stCls = /OK|COMPLETE/.test(st) ? 'st-ok' : /ERR|FAIL/.test(st) ? 'st-err' : 'st-run';
    const tid = esc(r.trace_id);
    return `<tr>
      <td><a href="/_apx/traces/${{tid}}">${{dt}}</a></td>
      <td class="dur">${{dur}}</td>
      <td class="dur" title="input+output token cost">${{cost}}</td>
      <td class="${{stCls}}">${{st}}</td>
      <td class="preview">${{esc((r.request_preview||'').slice(0,120))}}</td>
      <td class="preview">${{esc((r.response_preview||'').slice(0,120))}}</td>
      <td class="fb-cell">
        <input type="checkbox" class="td-pick" data-tid="${{tid}}" />
        <button class="fb-btn up" onclick="submitFeedback('${{tid}}',true,this)">👍</button>
        <button class="fb-btn down" onclick="submitFeedback('${{tid}}',false,this)">👎</button>
      </td>
    </tr>`;
  }}).join('');
  wrap.innerHTML = '<table>' + ths + tds + '</table>';
  refreshCompare();
}}

function selectedTids() {{
  return Array.from(document.querySelectorAll('.td-pick:checked'))
    .map(el => el.getAttribute('data-tid'))
    .filter(Boolean);
}}

function refreshCompare() {{
  const btn = document.getElementById('trace-diff-go');
  if (!btn) return;
  btn.disabled = selectedTids().length !== 2;
}}

document.addEventListener('DOMContentLoaded', async () => {{
  try {{
    const r = await fetch('/_apx/traces?fmt=json', {{headers: {{'Accept': 'application/json'}}}});
    if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
    const text = await r.text();
    let rows;
    try {{ rows = JSON.parse(text); }} catch(_) {{
      // Got HTML (auth redirect or error page) — reload to trigger SSO
      if (text.includes('<html') || text.includes('<!DOCTYPE')) {{ location.reload(); return; }}
      throw new Error('bad JSON response');
    }}
    renderRows(rows);
  }} catch(e) {{
    document.getElementById('traces-body').innerHTML =
      '<p class="empty">Failed to load traces: ' + e.message + '</p>';
  }}
  const go = document.getElementById('trace-diff-go');
  if (go) {{
    go.addEventListener('click', () => {{
      const tids = selectedTids();
      if (tids.length !== 2) return;
      location.href = '/_apx/traces/diff?a=' + encodeURIComponent(tids[0])
        + '&b=' + encodeURIComponent(tids[1]);
    }});
    document.addEventListener('change', (e) => {{
      if (!e.target || !e.target.classList || !e.target.classList.contains('td-pick')) return;
      const checked = document.querySelectorAll('.td-pick:checked');
      if (checked.length > 2) e.target.checked = false;
      refreshCompare();
    }});
  }}
  try {{
    const mount = document.getElementById('tool-fails');
    if (mount) {{
      const fr = await fetch('/_apx/traces/tool-fails', {{headers: {{'Accept': 'application/json'}}}});
      if (fr.ok) {{
        const d = await fr.json();
        const tools = Array.isArray(d.tools) ? d.tools : [];
        if (tools.length) {{
          mount.hidden = false;
          mount.innerHTML = tools.map(t => {{
            const name = esc(t.name);
            const failed = typeof t.failed === 'number' ? t.failed : 0;
            const total = typeof t.total === 'number' ? t.total : 0;
            const cls = failed > 0 ? 'st-err' : 'st-ok';
            return '<span class="tool-fail ' + cls + '">' + name + ': ' + failed + '/' + total + ' failed</span>';
          }}).join('');
        }}
      }}
    }}
  }} catch {{}}
}});
</script>
</head><body>
<header>
  <span class="badge">APX</span><h1>{_html.escape(title)}</h1>
  <a class="back" href="/_apx/agent">← Agent</a>
</header>
<main>
  <div id="tool-fails" class="tool-fails-card" hidden></div>
  <div id="trace-diff-bar" class="trace-diff-bar">
    <button id="trace-diff-go" class="trace-diff-go" type="button" disabled>Compare selected</button>
    <span>Pick two traces to compare responses and judge verdicts</span>
  </div>
  {body}
</main>
</body></html>"""


def _normalize_responses_api_spans(span_dicts: list[dict]) -> list[dict]:
    """Normalize Responses-API format spans to canonical chat-completions shape.

    Spans whose inputs carry an ``input`` list and whose outputs carry
    ``object == "response"`` are converted so the rest of the rendering
    pipeline sees one format regardless of which SDK produced the trace:

    * span_type  → CHAT_MODEL
    * inputs     → {"messages": [...]}
    * outputs    → {"choices": [{"message": {"role": "assistant", "content": ...}}]}
    * function_call / function_call_output pairs → synthetic TOOL child spans

    Returns a new list (may be longer than the input due to synthetic spans).
    """
    import json as _json

    result: list[dict] = []
    for span in span_dicts:
        inputs = span.get("inputs") or {}
        outputs = span.get("outputs") or {}
        input_list = inputs.get("input") if isinstance(inputs, dict) else None
        is_responses = (
            isinstance(input_list, list)
            and isinstance(outputs, dict)
            and outputs.get("object") == "response"
        )
        if not is_responses:
            result.append(span)
            continue

        output_items = outputs.get("output") or []

        # Normalize the span itself.
        normalized = dict(span)
        if normalized.get("span_type") in ("UNKNOWN", "OTHER"):
            normalized["span_type"] = "CHAT_MODEL"
        normalized["inputs"] = {"messages": input_list}

        # Final assistant text → choices format.
        final_text = ""
        for item in reversed(output_items):
            if item.get("type") == "message":
                content = item.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "output_text"
                    )
                final_text = content if isinstance(content, str) else ""
                break
        normalized["outputs"] = (
            {"choices": [{"message": {"role": "assistant", "content": final_text}}]}
            if final_text else {}
        )
        result.append(normalized)

        # Synthesize one TOOL span per function_call / function_call_output pair.
        tool_results = {
            item["call_id"]: item
            for item in output_items
            if item.get("type") == "function_call_output" and item.get("call_id")
        }
        for item in output_items:
            if item.get("type") != "function_call":
                continue
            call_id = item.get("call_id", "")
            result_item = tool_results.get(call_id) or {}
            raw_args = item.get("arguments", "")
            raw_out = result_item.get("output", "")
            try:
                tool_in = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                tool_in = {"query": raw_args}
            try:
                tool_out = _json.loads(raw_out) if isinstance(raw_out, str) else raw_out
            except Exception:
                tool_out = {"result": raw_out}
            result.append({
                "span_id": f"synth-{call_id}",
                "parent_id": span["span_id"],
                "name": item.get("name", "tool"),
                "span_type": "TOOL",
                "status": "OK",
                "start_time_ns": span.get("start_time_ns"),
                "end_time_ns": span.get("end_time_ns"),
                "duration_ms": None,
                "inputs": tool_in,
                "outputs": tool_out,
                "events": [],
            })

    return result


def _load_topology_node_metadata(start: Path | None = None) -> dict[str, dict[str, Any]]:
    root = (start or Path.cwd()).resolve()
    for directory in (root, *root.parents):
        path = directory / ".apx" / "topology_metadata.json"
        if not path.is_file():
            continue
        try:
            data = _json.loads(path.read_text())
        except Exception:
            logger.debug("topology metadata sidecar did not parse: %s", path, exc_info=True)
            return {}
        raw = data.get("node_metadata") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {
            str(node_id): dict(metadata)
            for node_id, metadata in raw.items()
            if isinstance(metadata, dict)
        }
    return {}


def _serialize_trace_spans(trace: Any) -> list[dict]:
    """Serialize an MLflow trace's spans into plain JSON-able dicts.

    Shared by the trace-detail route AND the in-process trace buffer
    (``_trace_store``) so both produce IDENTICAL span dicts. Normalizes
    Responses-API format spans to canonical chat-completions shape via
    ``_normalize_responses_api_spans`` so downstream renderers see one format.
    """
    spans = list(getattr(getattr(trace, "data", None), "spans", None) or [])
    span_dicts: list[dict] = []
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
            "events": [
                {
                    "name": getattr(e, "name", ""),
                    "attributes": {
                        k: str(v)
                        for k, v in (getattr(e, "attributes", None) or {}).items()
                    },
                }
                for e in (getattr(s, "events", None) or [])
            ],
        })
    return _normalize_responses_api_spans(span_dicts)


# ---------------------------------------------------------------------------
# Chat-payload rendering for the trace detail.
#
# LLM spans log their full ``{"messages": [...]}`` input and ``{"choices": [...]}``
# output. Because each step re-sends the whole conversation, dumping the raw
# JSON re-prints the system prompt + every prior turn at every level — the
# trace reads as if it repeats itself as you descend. These helpers render the
# payloads as a compact, role-labeled conversation and elide the prefix already
# shown one level up, so each span shows only what's *new*.
# ---------------------------------------------------------------------------

_MSG_CONTENT_CAP = 600  # truncate a single message body in the conversation view
_SYS_PREVIEW = 80       # chars of a folded system prompt shown in its summary line


def _is_chat_messages(obj: Any) -> list | None:
    """If ``obj`` is a chat-input payload, return its ``messages`` list, else None."""
    if isinstance(obj, dict):
        msgs = obj.get("messages")
        if isinstance(msgs, list) and msgs and all(isinstance(m, dict) for m in msgs):
            return msgs
    return None


def _is_choices(obj: Any) -> list | None:
    """If ``obj`` is a chat-output payload, return its ``choices`` list, else None."""
    if isinstance(obj, dict):
        ch = obj.get("choices")
        if isinstance(ch, list) and ch and all(isinstance(c, dict) for c in ch):
            return ch
    return None




def _drop_warmup_traces(traces: list) -> list:
    """Filter out the startup ``apx.trace_capture.warmup`` self-test traces.

    That trace materializes the OTel provider at startup and carries a single
    internal span — opened from the list it reads as an empty trace, so it's
    confusing noise. Identified by the ``mlflow.traceName`` tag. Tolerant of
    missing/None tags (keeps the trace).
    """
    from ._trace_store import WARMUP_SPAN_NAME

    out = []
    for t in traces:
        tags = getattr(getattr(t, "info", None), "tags", None) or {}
        if tags.get("mlflow.traceName") == WARMUP_SPAN_NAME:
            continue
        out.append(t)
    return out


def _workspace_host_from_request(request: Request) -> str | None:
    """Best-effort Databricks workspace host for deep links."""
    try:
        from ._defaults import _ws_prefer_obo

        ws = _ws_prefer_obo(request)
        host = (getattr(getattr(ws, "config", None), "host", None) or "").rstrip("/")
        if host:
            return host
    except Exception:
        pass
    host = (os.environ.get("DATABRICKS_HOST") or "").rstrip("/")
    return host or None


def _experiment_url(host: str | None, experiment_id: str | None) -> str | None:
    if not host or not experiment_id:
        return None
    return f"{host}/ml/experiments/{experiment_id}"


def _app_url(host: str | None, app_name: str | None) -> str | None:
    """The App's page in the workspace UI (the same link ``apx agents deploy`` prints)."""
    if not host or not app_name:
        return None
    from urllib.parse import quote

    return f"{host}/apps/{quote(app_name, safe='')}"


def _agent_resource_refs(ctx: AgentContext | None) -> list[dict[str, Any]]:
    """The agent's declared resources, each with its workspace-graph node id (or None)."""
    if ctx is None:
        return []
    try:
        from ._resources import collect_resource_specs

        specs = collect_resource_specs(ctx.agent)
    except Exception:  # noqa: BLE001 — the context pages degrade to "no resources", never 500
        logger.debug("collect_resource_specs failed", exc_info=True)
        return []
    from .workspace_graph.schema import resource_node_id

    return [{"kind": s.kind, "identifier": s.identifier, "node_id": resource_node_id(s.kind, s.identifier)}
            for s in specs]


def _overlay_agent(payload: dict[str, Any], ctx: AgentContext | None) -> dict[str, Any]:
    """Put this agent into a Discover graph payload: its node, ``uses`` edges, footprint.

    Deployed, the apps connector already drew the App node (and its configured
    resources); it becomes the agent. Locally a synthesized ``Agent`` node stands in.
    Declared resources not in the graph (out of scope, or not a graph type) are ``unplaced``.
    """
    from .workspace_graph.schema import node_id

    ids = {n["id"] for n in payload["nodes"]}
    app_name = os.environ.get("DATABRICKS_APP_NAME")
    agent_id = node_id("app", app_name) if app_name and node_id("app", app_name) in ids else "agent:self"
    if agent_id == "agent:self":
        name = ctx.config.name if ctx is not None else "This agent"
        payload["nodes"].append({"id": agent_id, "type": "Agent", "name": name, "fqn": name, "props": {}})
    drawn = {e["dst"] for e in payload["edges"] if e["src"] == agent_id and e["type"] == "uses"}
    footprint: list[str] = []
    unplaced: list[dict[str, Any]] = []
    for ref in _agent_resource_refs(ctx):
        target = ref["node_id"]
        if target is None or target not in ids:
            unplaced.append(ref)
            continue
        footprint.append(target)
        if target not in drawn:
            payload["edges"].append({"src": agent_id, "dst": target, "type": "uses"})
            drawn.add(target)
    return {"id": agent_id, "footprint": footprint, "unplaced": unplaced}


def _lookup_experiment_name(experiment_id: str) -> str | None:
    try:
        from mlflow.tracking import MlflowClient

        exp = MlflowClient().get_experiment(experiment_id)
        return getattr(exp, "name", None) or None
    except Exception:
        return None


class _BufferedSpans(NamedTuple):
    trace_id: str | None
    spans: list[dict[str, Any]]


def _latest_buffered_spans() -> _BufferedSpans:
    """Return ``(trace_id, spans)`` for the newest non-warmup buffered trace."""
    from ._trace_store import WARMUP_SPAN_NAME, get as _ts_get, list_recent as _ts_list

    for tid in _ts_list(20):
        spans = _ts_get(tid) or []
        if any((s.get("name") or "") == WARMUP_SPAN_NAME for s in spans):
            continue
        if spans:
            return _BufferedSpans(tid, spans)
    return _BufferedSpans(None, [])


def _is_message_heavy(obj: Any) -> bool:
    """True if ``obj`` is (or wraps) a chat message list.

    The conversation lives uniformly on the LLM spans. The CHAIN/AGENT wrapper
    spans (LangGraph, model, tools) re-log the same growing message list in a
    different (LangChain ``type``) shape at every nesting level — that re-log IS
    the "repeats itself as you go down a level" noise. We detect such payloads
    so they can be suppressed on non-LLM spans and shown once, on the LLM span.
    """
    if _is_chat_messages(obj) is not None or _is_choices(obj) is not None:
        return True
    if isinstance(obj, dict):
        # LangGraph state updates: {"update": {"messages": [...]}, "goto": ...}
        for v in obj.values():
            if isinstance(v, dict) and isinstance(v.get("messages"), list):
                return True
    if (
        isinstance(obj, list) and obj and isinstance(obj[0], dict)
        and ("args" in obj[0] or "tool_calls" in obj[0])
    ):
        return True  # the bare tool_call list logged on `tools` wrapper spans
    return False


def _common_prefix_len(a: list, b: list) -> int:
    """Number of leading elements ``a`` and ``b`` share (by equality)."""
    n = 0
    for x, y in zip(a, b):
        if x == y:
            n += 1
        else:
            break
    return n


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f" … (+{len(text) - cap} chars)"


def _render_message_line(m: dict) -> str:
    """Render one chat message as a compact role-labeled row.

    System prompts fold behind a native ``<details>`` disclosure (they're long
    and identical across calls); assistant tool calls render as
    ``→ run_sql({...})``; everything else shows a truncated one-liner.
    """
    import html as _html
    import json as _json

    role = str(m.get("role") or "?")
    content = m.get("content")
    tool_calls = m.get("tool_calls")

    if role == "system" and isinstance(content, str):
        n = len(content)
        preview = _html.escape(content[:_SYS_PREVIEW].replace("\n", " "))
        return (
            f'<details class="tmsg tsys"><summary>'
            f'<span class="trole">system</span> {preview}… ({n} chars)'
            f'</summary><pre class="tpre">{_html.escape(content)}</pre></details>'
        )

    if tool_calls and isinstance(tool_calls, list):
        rows = ""
        for tc in tool_calls:
            fn = (tc or {}).get("function") if isinstance(tc, dict) else None
            name = (fn or {}).get("name", "tool") if isinstance(fn, dict) else "tool"
            args = (fn or {}).get("arguments", "") if isinstance(fn, dict) else ""
            if not isinstance(args, str):
                args = _json.dumps(args)
            rows += (
                f'<div class="tmsg"><span class="trole">→ {_html.escape(str(name))}</span>'
                f'<span class="tcontent">{_html.escape(_truncate(args, _MSG_CONTENT_CAP))}</span></div>'
            )
        return rows

    if content is None:
        content = ""
    if not isinstance(content, str):
        content = _json.dumps(content)
    return (
        f'<div class="tmsg"><span class="trole">{_html.escape(role)}</span>'
        f'<span class="tcontent">{_html.escape(_truncate(content, _MSG_CONTENT_CAP))}</span></div>'
    )


def _render_messages_block(messages: list, prev: list | None) -> str:
    """Render a ``messages`` list, collapsing the prefix shared with ``prev``.

    The shared leading messages (system prompt + prior turns already shown one
    level up) fold into a single expandable ``↳ + N earlier messages`` line;
    only the new tail renders expanded.
    """
    k = _common_prefix_len(prev or [], messages)
    html = ""
    if k > 0:
        earlier = "".join(_render_message_line(m) for m in messages[:k])
        plural = "s" if k != 1 else ""
        html += (
            f'<details class="tprefix"><summary>↳ + {k} earlier message{plural} '
            f'(same as above)</summary>{earlier}</details>'
        )
    html += "".join(_render_message_line(m) for m in messages[k:])
    return html


def _render_choices_block(choices: list) -> str:
    """Render assistant output ``choices`` as compact message rows."""
    html = ""
    for ch in choices:
        msg = ch.get("message") if isinstance(ch, dict) else None
        if isinstance(msg, dict):
            html += _render_message_line(msg)
    return html


def _render_trace_detail(trace_id: str, spans: list | None, error: str | None) -> str:
    import json as _json
    import html as _html

    from ._ui_table import render_tool_output_html

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

        # Running conversation state across the depth-first walk: the last
        # messages list rendered. A nested LLM span re-sends the whole
        # conversation, so we collapse the prefix it shares with what was shown
        # one step earlier and render only the new tail (the "show what's new"
        # delta view) — DFS order matches execution order.
        state: dict = {"prev": None}

        def _render_span(s: dict, depth: int = 0) -> str:
            st = _span_type_css(s.get("span_type", ""))
            dur = f"{s['duration_ms']}ms" if s.get("duration_ms") is not None else "—"
            status = s.get("status", "")
            st_cls = "sstatus-ok" if "OK" in status.upper() else "sstatus-err"
            name = _html.escape(s.get("name", ""))
            _html.escape(s.get("span_id", ""))
            inputs_obj = s.get("inputs")
            outputs_obj = s.get("outputs")
            io_html = ""

            # ── Inputs ────────────────────────────────────────────────────────
            # Conversation view renders only on LLM spans — CHAIN/AGENT wrappers
            # re-log the same growing message list in a different shape, which is
            # the "repeats itself going down" noise. Non-LLM spans with message
            # inputs are silently suppressed (they're message-heavy, so the elif
            # branch also skips the raw-JSON dump).
            msgs = _is_chat_messages(inputs_obj)
            if msgs is not None and st == "LLM":
                io_html += (
                    '<div class="io-block"><div class="io-label">Messages</div>'
                    f'<div class="convo">{_render_messages_block(msgs, state["prev"])}</div></div>'
                )
                state["prev"] = msgs
            elif inputs_obj and not _is_message_heavy(inputs_obj):
                cleaned = {k: v for k, v in inputs_obj.items() if v is not None} if isinstance(inputs_obj, dict) else inputs_obj
                inp = _json.dumps(cleaned, indent=2)
                io_html += f'<div class="io-block"><div class="io-label">Inputs</div><pre class="io-pre">{_html.escape(inp[:4000])}</pre></div>'

            # ── Outputs ───────────────────────────────────────────────────────
            choices = _is_choices(outputs_obj)
            if choices is not None:
                io_html += (
                    '<div class="io-block"><div class="io-label">Response</div>'
                    f'<div class="convo">{_render_choices_block(choices)}</div></div>'
                )
                added = [c["message"] for c in choices
                         if isinstance(c, dict) and isinstance(c.get("message"), dict)]
                if added:
                    state["prev"] = (state["prev"] or []) + added
            elif outputs_obj and not _is_message_heavy(outputs_obj):
                cleaned = {k: v for k, v in outputs_obj.items() if v is not None} if isinstance(outputs_obj, dict) else outputs_obj
                # Tool responses are the tabular payloads in a trace. Agent /
                # chain state dumps (LangGraph updates, message lists) keep the
                # raw-JSON view — their shape is the diagnostic, and the
                # conversation view above already renders the messages.
                table_html = render_tool_output_html(cleaned) if st == "TOOL" else None
                if table_html:
                    io_html += f'<div class="io-block"><div class="io-label">Outputs</div>{table_html}</div>'
                else:
                    out = _json.dumps(cleaned, indent=2)
                    io_html += f'<div class="io-block"><div class="io-label">Outputs</div><pre class="io-pre">{_html.escape(out[:4000])}</pre></div>'
            # Progress markers (span events) render in an always-visible strip
            # so a cold-start step shows even while the body is collapsed.
            events_html = ""
            for ev in (s.get("events") or []):
                # Streaming logs one 'new_token' event per generated token —
                # hundreds per span. They carry no diagnostic value here and
                # bury the real spans; skip them.
                if ev.get("name") == "new_token":
                    continue
                attrs = ev.get("attributes") or {}
                # MLflow records failures as the standard OpenTelemetry
                # ``exception`` event.  Those events do not have the generic
                # ``message`` attribute used by progress events, so rendering
                # only the event name hid the actual exception from the trace
                # detail view.
                if ev.get("name") == "exception" or "exception.message" in attrs:
                    exc_type = attrs.get("exception.type")
                    exc_message = attrs.get("exception.message")
                    summary = ": ".join(
                        str(part) for part in (exc_type, exc_message) if part
                    ) or "exception"
                    events_html += (
                        f'<div class="span-event">▸ {_html.escape(summary)}</div>'
                    )
                    stacktrace = attrs.get("exception.stacktrace")
                    if stacktrace:
                        events_html += (
                            '<pre class="span-event exception-stacktrace">'
                            f'{_html.escape(str(stacktrace))}</pre>'
                        )
                else:
                    msg = attrs.get("message") or ev.get("name", "")
                    events_html += f'<div class="span-event">▸ {_html.escape(msg)}</div>'
            kids = "".join(_render_span(c, depth + 1) for c in children.get(s.get("span_id", ""), []))
            indent = f'<div class="indent">{kids}</div>' if kids else ""
            return (
                f'<div class="span-card">'
                f'<div class="span-head" onclick="this.parentElement.querySelector(\'.span-body\').classList.toggle(\'open\')">'
                f'<span class="stype stype-{st}">{st}</span>'
                f'<span class="sname">{name}</span>'
                f'<span class="sdur">{dur}</span>'
                f'<span class="sstatus {st_cls}">{status}</span>'
                f'</div>'
                f'{events_html}'
                f'<div class="span-body">{io_html}</div>'
                f'</div>'
                f'{indent}'
            )

        tree_html = '<div class="span-tree">' + "".join(_render_span(s) for s in roots) + "</div>"
        body = err_html + tree_html

    tid_escaped = _html.escape(trace_id)
    tid_js = tid_escaped.replace("'", "\\'")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Trace {tid_escaped}</title>
{apx_theme_style()}
<style>{_TRACE_CSS}
.feedback{{display:flex;flex-direction:column;gap:.4rem;margin-top:.5rem}}
.fb-row{{display:flex;align-items:center;gap:.5rem}}
.fb-btn{{background:none;border:1px solid var(--border,var(--apx-border));border-radius:4px;cursor:pointer;font-size:1rem;padding:.1rem .5rem;opacity:.6}}
.fb-btn:hover{{opacity:1}}
.fb-btn.active{{opacity:1;border-color:currentColor}}
.fb-btn.up.active{{color:var(--apx-ok)}}
.fb-btn.down.active{{color:var(--apx-err)}}
#fb-msg{{font-size:.75rem;color:var(--apx-muted)}}
#fb-rationale{{width:100%;max-width:520px;background:var(--apx-panel-2);border:1px solid var(--apx-border);color:var(--apx-text);border-radius:4px;padding:5px 8px;font-size:12px;font-family:inherit;resize:vertical;min-height:48px;display:none}}
#fb-rationale.visible{{display:block}}
.fb-save-btn{{background:var(--apx-ok-soft);border:1px solid var(--apx-ok);color:var(--apx-ok);border-radius:4px;padding:3px 12px;font-size:12px;cursor:pointer;display:none}}
.fb-save-btn.visible{{display:inline-block}}
.fb-save-btn:hover{{background:var(--apx-ok);color:var(--apx-on-accent)}}
.fb-screenshot{{margin-top:8px;max-width:480px;border-radius:4px;border:1px solid var(--apx-border)}}
#fb-prev-note{{font-size:11px;color:var(--apx-muted);font-style:italic}}
.fb-hint{{font-size:11px;color:var(--apx-muted);margin-top:2px}}
</style></head><body>
<header>
  <span class="badge">APX</span><h1>Trace</h1>
  <a class="back" href="/_apx/traces">← All traces</a>
</header>
<main>
  <div class="meta">ID: {tid_escaped}
    <div class="feedback">
      <div class="fb-row">
        <button class="fb-btn up" title="Good response" onclick="toggleRationale(true,this)">👍</button>
        <button class="fb-btn down" title="Bad response" onclick="toggleRationale(false,this)">👎</button>
      </div>
      <textarea id="fb-rationale" placeholder="Why? (e.g. 'fabricated a number', 'correct SQL, right answer')"></textarea>
      <div style="display:flex;align-items:center;gap:.5rem;margin-top:4px">
        <button class="fb-save-btn" id="fb-save-btn" onclick="submitFeedback()">Save</button>
        <span id="fb-msg"></span>
      </div>
      <div class="fb-hint" id="fb-hint" style="display:none">
        Saved to MLflow as a <code>quality</code> assessment on this trace.
        Collect ~20 ratings then go to <a href="/_apx/eval" style="color:var(--apx-accent)">Eval → Judge Alignment</a> to run MemAlign.
      </div>
      <div id="fb-prev-note"></div>
      <div id="fb-screenshot-wrap"></div>
    </div>
  </div>
  {body}
</main>
<script>
let _pendingValue = null;

function toggleRationale(value, btn) {{
  _pendingValue = value;
  document.querySelectorAll('.fb-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('fb-rationale').classList.add('visible');
  document.getElementById('fb-save-btn').classList.add('visible');
  document.getElementById('fb-rationale').focus();
}}

async function submitFeedback() {{
  const msg = document.getElementById('fb-msg');
  const comment = document.getElementById('fb-rationale').value.trim() || null;
  msg.textContent = 'Saving…';
  try {{
    const r = await fetch('/_apx/feedback', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{trace_id: '{tid_js}', name: 'quality', value: _pendingValue, comment, idempotency_key: '{tid_js}'}})
    }});
    if (!r.ok) throw new Error(await r.text());
    msg.textContent = (_pendingValue ? '✓ Marked good' : '✓ Marked bad') + (comment ? ' · note saved' : '');
    document.getElementById('fb-rationale').classList.remove('visible');
    document.getElementById('fb-save-btn').classList.remove('visible');
    document.getElementById('fb-hint').style.display = 'block';
    // Bust cache so new rating appears immediately in Eval page
    fetch('/_apx/eval/cache/bust', {{method:'POST'}}).catch(()=>{{}});
  }} catch(e) {{
    msg.textContent = 'Failed: ' + e.message;
  }}
}}

document.addEventListener('DOMContentLoaded', async () => {{
  // Submit on Enter (without shift), allow shift+Enter for newlines
  document.getElementById('fb-rationale').addEventListener('keydown', e => {{
    if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); submitFeedback(); }}
  }});

  try {{
    const r = await fetch('/_apx/feedback/{tid_js}');
    if (!r.ok) return;
    const data = await r.json();
    const quality = (data.assessments || []).filter(a => a.name === 'quality').pop();
    if (!quality) return;
    const cls = quality.value === true ? 'up' : 'down';
    document.querySelector('.fb-btn.' + cls)?.classList.add('active');
    if (quality.rationale) {{
      document.getElementById('fb-prev-note').textContent = '↩ ' + quality.rationale;
    }}
    const uri = (quality.metadata || {{}}).screenshot_uri;
    if (uri) {{
      const wrap = document.getElementById('fb-screenshot-wrap');
      const img = document.createElement('img');
      img.src = uri; img.className = 'fb-screenshot';
      img.alt = 'annotation screenshot';
      wrap.appendChild(img);
    }}
  }} catch(_) {{}}
}});
</script>
</body></html>"""


def _parse_judge_output(text: str) -> _JudgeOutput:
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
        return _JudgeOutput(verdict=verdict, reason="No output from judge model")

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

    # No VERDICT line at all → keep FAIL. An eval harness must fail closed on
    # unclear judge output (the docstring's "fall back to FAIL" contract); the
    # previous substring inference flipped "passable"/"would pass"/"COMPASSIONATE"
    # to PASS, the worst direction for an eval (audit M4).

    return _JudgeOutput(verdict=verdict, reason=reason or "(no reason provided)")


# ---------------------------------------------------------------------------
# Dev-UI write authorization (audit H17/H18 / #611)
#
# The dev router hosts code-write/RCE-equivalent endpoints (POST /_apx/edit,
# /_apx/tools/new, /_apx/tools/suggest, /_apx/replay/*, save_setup, env writes,
# DELETE /_apx/tools/{name}) and an SSRF probe (/_apx/setup/probe-json). Those
# write/probe endpoints must not be reachable without a signed-in Apps user.
#
# Posture:
#   * Local ``apx-agent run`` (no DATABRICKS_APP_PORT) → writes allowed (the
#     dev loop).
#   * Deployed Databricks App (DATABRICKS_APP_PORT set):
#       - ``X-Forwarded-Access-Token`` present (Apps SSO / OBO) → allow
#         ordinary Dev-UI writes (edit, setup, grounding, …).
#       - Else if ``APX_DEV_UI_TOKEN`` is set and matches ``X-APX-Dev-Token``
#         (or ``?token=``) → allow (CI / non-browser automation).
#       - Else → 403.
#
# Browser Dev UI ordinary writes therefore work for any signed-in App user —
# no pasted operator token.
# The guard is attached once at the router level and only enforces on
# state-changing methods plus the SSRF probe / per-principal data reads, so
# benign read GETs stay open.
#
# Discover / Setup inventory GETs (#629) are intentionally ungated beyond OBO:
# any signed-in Apps user may enumerate resources *visible to them* under their
# own grants (caller-scoped discovery). That is not App-SP recon — handlers use
# ``_ws_prefer_obo`` and fail closed without OBO (#612). Requiring
# ``APX_DEV_UI_TOKEN`` for inventory would break Discover browse for ordinary
# App users. Discover is read-only; there are no Discover mutations.
# ---------------------------------------------------------------------------

_DEV_TOKEN_HEADER = "x-apx-dev-token"
_OBO_TOKEN_HEADER = "x-forwarded-access-token"
_DEV_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Read GETs that expose per-principal data (approvals, conversations + their
# items, memories, traces). Gated like writes on a deployed App so anonymous
# callers cannot scrape them; signed-in Apps SSO is enough (#468 tradeoff:
# any App viewer can read until these routes are principal-scoped).
# Benign reads (chat HTML, topology, probe, setup discovery) stay open so
# end-user chat and diagnostics still work without auth gymnastics.
_DEV_DATA_READ_SEGMENTS = ("/approvals", "/conversations", "/memories", "/traces")


def _is_deployed_app() -> bool:
    """True when running inside a deployed Databricks App.

    Env-only (``DATABRICKS_APP_PORT``) deliberately — a local ``apx-agent run`` that
    happens to sit behind a proxy must not be locked out, so the X-Forwarded-*
    heuristic used elsewhere is not consulted here.
    """
    return bool(os.environ.get("DATABRICKS_APP_PORT"))


def _supplied_dev_token(request: Request) -> str:
    return (
        request.headers.get(_DEV_TOKEN_HEADER)
        or request.query_params.get("token")
        or ""
    )


def _enforce_dev_write_auth(request: Request) -> None:
    """Authorize a dev-UI write/probe request, or raise 403.

    See the module-level posture comment for the full decision table.
    """
    import hmac

    if not _is_deployed_app():
        return

    # Apps SSO: proxy injects the caller's OBO token on every browser request.
    obo = (request.headers.get(_OBO_TOKEN_HEADER) or "").strip()
    if obo:
        return

    # Optional shared secret for non-browser automation (CI, curl).
    raw = os.environ.get("APX_DEV_UI_TOKEN")
    token = raw.strip() if raw else ""
    if token:
        supplied = _supplied_dev_token(request)
        if supplied and hmac.compare_digest(supplied, token):
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "Dev-UI writes require Apps SSO (X-Forwarded-Access-Token) "
                "or a matching X-APX-Dev-Token."
            ),
        )

    raise HTTPException(
        status_code=403,
        detail=(
            "Dev-UI write endpoints require a signed-in Databricks Apps user "
            "(SSO). Open this App in the browser while logged in."
        ),
    )


async def _dev_write_guard(request: Request) -> None:
    """Router-level dependency: gate state-changing methods, the SSRF probe, and
    per-principal data reads.

    Attached at router construction so it covers every current and future
    write route (edit, tools/new, tools/suggest, replay/*, setup writes,
    DELETE tools) without per-route annotations that could miss one. Benign read
    GETs (chat HTML, topology, probe/checks, setup/catalogs, Discover inventory,
    …) fall through so end-user chat and diagnostics work without the operator
    token — intentional under OBO grants (#629), not an oversight.
    """
    # Two side-effecting GETs need gating too: the SSRF probe and
    # /_apx/deploy/stream, which spawns ``apx-agent deploy`` as a subprocess
    # (privileged, state-changing — same threat class as the write endpoints).
    path = request.url.path
    is_side_effecting_get = path.endswith("/setup/probe-json") or path.endswith(
        "/deploy/stream"
    )
    # Per-principal data reads (#468): on a deployed App these would otherwise
    # let any authorized viewer read every principal's approvals / conversations
    # / memories / traces. Gate them with the same operator token as writes.
    is_data_read = request.method == "GET" and any(
        seg in path for seg in _DEV_DATA_READ_SEGMENTS
    )
    if request.method in _DEV_WRITE_METHODS or is_side_effecting_get or is_data_read:
        _enforce_dev_write_auth(request)


def inject_create_tool_meta(ctx: AgentContext) -> None:
    """Inject the create_tool meta-tool for dev mode."""
    _create_tool_meta = AgentTool(
        name="create_tool",
        description=(
            "Create a new tool for this agent from a natural language description. "
            "Call this when the user asks to add a new capability, tool, or function to the agent. "
            "The tool is appended to agent.py; restart `apx-agent run` (or redeploy) to load it."
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
        "The tool will be generated and inserted into agent_router.py; restart `apx-agent run` to load it."
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
        # Hot-apply onto the live leaf so Chat picks up the change without restart.
        try:
            from ._discover_hot import resolve_live_leaf

            leaf = resolve_live_leaf(ctx.agent, "agent") if ctx.agent is not None else None
            if leaf is not None:
                leaf._instructions = instructions
        except Exception:
            logger.debug("live instructions hot-apply skipped", exc_info=True)

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


def _load_optimize_eval_rows() -> list[Any]:
    """Eval rows for optimize-instructions — same source as GET /_apx/eval/data.

    MLflow cache when fresh (and an experiment is set), else the local
    ``evals.json`` via ``_find_evals_path``. Returns ``[]`` when neither yields
    rows so the caller can fail clear without ever calling ``optimize_prompts``.
    """
    experiment_id = (os.environ.get("MLFLOW_EXPERIMENT_ID") or "").strip()
    if experiment_id and _EVAL_CASES_CACHE.fresh:
        cached = _EVAL_CASES_CACHE.get()
        if cached:
            return cached
    path = _find_evals_path()
    if path is None or not path.exists():
        return []
    try:
        data = _json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _current_root_instructions(ctx: "AgentContext | None") -> str:
    """The root agent's current instructions — the text the editor shows and
    Save persists. Prefers the on-disk ``agent.py`` (the source of truth), and
    falls back to the live config with the dev-mode addendum stripped."""
    from ._ui_edit import _find_agent_router_path, _parse_agent_nodes

    path = _find_agent_router_path()
    if path and path.exists():
        for node in _parse_agent_nodes(path.read_text()):
            if node["name"] == "agent" and node["instructions"]:
                return node["instructions"]
    instr = (ctx.config.instructions if ctx else "") or ""
    if "[DEV MODE]" in instr:
        instr = instr.split("[DEV MODE]", 1)[0].rstrip()
    return instr


def _instruction_target(agent: Any, node: str, path: Path) -> Any:
    """Resolve an editor variable to a reachable, local instruction-bearing agent."""
    import sys
    from ._agents import LlmAgent, LoopAgent
    from ._topology import _iter_child_agents

    matches = [vars(module).get(node) for module in list(sys.modules.values())
               if module is not None and vars(module).get("__file__") == str(path)]
    target = agent if node == "agent" else next((value for value in matches if value is not None), None)
    while isinstance(target, LoopAgent):
        target = target._inner
    pending, seen = [agent], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if current is target and isinstance(target, LlmAgent):
            if target._name in getattr(agent, "_apx_remote_leaf_bindings", {}):
                raise ValueError(f"Instruction node {node!r} runs remotely; edit and evaluate it in its owning project")
            return target
        pending.extend(child for _, child in _iter_child_agents(current))
    raise ValueError(f"Instruction node {node!r} is not a local leaf in the running agent. Restart after changing composition.")


def _instruction_candidate_agent(agent: Any, target: Any, instructions: str) -> Any:
    """Copy the declaration graph, preserving tool/config objects and live instructions."""
    from copy import copy, deepcopy
    from ._agents import LoopAgent
    from ._topology import _iter_child_agents

    while isinstance(target, LoopAgent):
        target = target._inner
    pending, nodes = [agent], {}
    while pending:
        current = pending.pop()
        if id(current) in nodes:
            continue
        nodes[id(current)] = current
        pending.extend(child for _, child in _iter_child_agents(current))
    if id(target) not in nodes:
        raise ValueError("Selected instruction node is not in the running agent")
    copies = {key: copy(value) for key, value in nodes.items()}
    for key, original in nodes.items():
        for attr in ("_agents", "_routes", "_branches", "_inner", "_default"):
            if hasattr(original, attr):
                setattr(copies[key], attr, deepcopy(getattr(original, attr), dict(copies)))
    copies[id(target)]._instructions = instructions
    return copies[id(agent)]


def _optimize_instructions_sync(
    *,
    judge_name: str,
    rows: list[Any],
    agent: Any,
    model: str,
    current_instructions: str,
    experiment_id: str | None,
    reflection_model: str,
    target: Any = None,
) -> dict[str, Any]:
    """Run GEPA prompt optimization against the eval rows + judge (synchronous —
    called via ``asyncio.to_thread``).

    Bridges the current instructions through a *transient* MLflow prompt: it is
    registered, passed as the single ``prompt_uris`` entry, and deleted in a
    ``finally`` so cleanup runs even if ``optimize_prompts`` raises. The prompt
    registry is never surfaced and never a source of truth. Returns a dict with
    a ``status`` key the route strips before responding.
    """
    import uuid

    from mlflow import MlflowClient
    from mlflow.genai import load_prompt, optimize_prompts, register_prompt
    from mlflow.genai.optimize import GepaPromptOptimizer
    from mlflow.genai.scorers import get_scorer

    from apx_agent import _eval

    try:
        judge = get_scorer(name=judge_name, experiment_id=experiment_id)
    except Exception as exc:  # noqa: BLE001 — unknown/unavailable judge → clear error, no optimize call
        return {"ok": False, "error": f"Judge {judge_name!r} is not available: {exc}", "status": 422}

    def predict_fn(inputs: Any) -> str:
        from mlflow.types.agent import ChatAgentMessage
        from apx_agent import compile_to_chat_agent

        # MLflow substitutes candidate templates at load_prompt().template.
        # Compile each prediction independently so concurrent trials cannot
        # reuse stale instructions or mutate the served agent.
        template = load_prompt(uri).template
        if not isinstance(template, str):
            raise ValueError("Instruction optimization requires a text prompt")
        candidate = _instruction_candidate_agent(agent, agent if target is None else target, template)
        compiled = compile_to_chat_agent(candidate, model=model)
        chat_messages = [
            ChatAgentMessage(role=m.get("role", "user"), content=m.get("content", ""), id=m.get("id"))
            for m in _eval._extract_messages(inputs)
        ]
        return _eval._extract_response_text(compiled.predict(chat_messages))

    prompt_name = f"apx_optimize_{uuid.uuid4().hex}"
    registered = register_prompt(name=prompt_name, template=current_instructions)
    uri = f"prompts:/{registered.name}/{registered.version}"
    try:
        result = optimize_prompts(
            predict_fn=predict_fn,
            train_data=_eval._normalize_evalset(rows),
            prompt_uris=[uri],
            optimizer=GepaPromptOptimizer(reflection_model=reflection_model),
            scorers=[judge],
        )
        optimized = result.optimized_prompts or []
        candidate = optimized[0].template if optimized else current_instructions
        if not isinstance(candidate, str):
            raise ValueError("Optimized instructions must be text")
        return {
            "ok": True,
            "candidate": candidate,
            "scores": {"before": result.initial_eval_score, "after": result.final_eval_score},
            "status": 200,
        }
    finally:
        try:
            MlflowClient().delete_prompt(prompt_name)
        except Exception:  # noqa: BLE001 — best-effort cleanup of the transient bridge prompt
            logger.warning("optimize-instructions: transient prompt cleanup failed for %s", prompt_name, exc_info=True)


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


def _pick_workspace_defaults(ws: WorkspaceClient) -> "dict[str, str]":
    """Best-effort: pick a sensible (DEMO_CATALOG, DEMO_SCHEMA, WAREHOUSE_ID)
    for the Setup page when the user hasn't configured one yet.

    Strategy:
    - Catalog/schema: scan user-accessible catalogs and pick the first one
      with a non-empty, non-system schema. The ``samples`` demo catalog is
      kept as a fallback so a brand-new workspace still lands on something
      readable, but any *real* user catalog wins over ``samples``.
    - Warehouse: prefer a ``RUNNING`` one (no cold-start hit), else any
      serverless one, else just the first warehouse.

    All SDK calls are blocking — call this from a thread pool via
    ``asyncio.to_thread``. Returns ``{}`` if the workspace can't be probed.
    """
    out: dict[str, str] = {}
    skip_cat = {"system", "__databricks_internal"}
    skip_sch = {"information_schema"}

    try:
        catalogs = list(ws.catalogs.list())
    except Exception:
        catalogs = []

    samples_cat = next((c for c in catalogs if c.name == "samples"), None)

    chosen_cat: str | None = None
    chosen_sch: str | None = None
    for cat in catalogs:
        cname = cat.name or ""
        if not cname or cname in skip_cat or cname == "samples":
            continue
        try:
            schemas = list(ws.schemas.list(catalog_name=cname))
        except Exception:
            continue
        for sch in schemas:
            sname = sch.name or ""
            if not sname or sname in skip_sch:
                continue
            chosen_cat, chosen_sch = cname, sname
            break
        if chosen_cat:
            break

    if not chosen_cat and samples_cat is not None:
        chosen_cat = "samples"
        try:
            schemas = list(ws.schemas.list(catalog_name="samples"))
            for sch in schemas:
                if sch.name and sch.name not in skip_sch:
                    chosen_sch = sch.name
                    if sch.name == "nyctaxi":
                        break
        except Exception:
            chosen_sch = "nyctaxi"

    if chosen_cat:
        out["DEMO_CATALOG"] = chosen_cat
    if chosen_sch:
        out["DEMO_SCHEMA"] = chosen_sch

    try:
        warehouses = list(ws.warehouses.list())
    except Exception:
        warehouses = []

    def _running(w: Any) -> bool:
        return getattr(getattr(w, "state", None), "value", str(getattr(w, "state", ""))) == "RUNNING"

    def _serverless(w: Any) -> bool:
        return bool(getattr(w, "enable_serverless_compute", False))

    pick = next((w for w in warehouses if w.id and _running(w)), None)
    if pick is None:
        pick = next((w for w in warehouses if w.id and _serverless(w)), None)
    if pick is None:
        pick = next((w for w in warehouses if w.id), None)
    if pick is not None and pick.id:
        out["WAREHOUSE_ID"] = pick.id

    return out


# ── MLflow fetch helpers (run in thread pool to avoid blocking the event loop) ─

def _fetch_eval_cases_sync(experiment_id: str) -> list[dict[str, Any]]:
    """Fetch MLflow-labeled traces and map to eval cases (sync, call via executor)."""
    try:
        import mlflow as _mlflow
        df = _mlflow.search_traces(
            experiment_ids=[experiment_id],
            max_results=100,
            order_by=["attributes.start_time DESC"],
        )
        cases: list[dict[str, Any]] = []
        for _, row in df.iterrows():  # type: ignore[union-attr]
            assessments = row.get("assessments") or []
            quality = next(
                (a for a in assessments
                 if (a.get("assessment_name") if isinstance(a, dict) else getattr(a, "name", None)) == "quality"),
                None,
            )
            if quality is None:
                continue
            req = row.get("request") or ""
            question = ""
            try:
                req_obj = _json.loads(req) if isinstance(req, str) else req
                inputs = req_obj.get("input") or req_obj.get("messages") or []
                for msg in inputs:
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        question = str(msg.get("content", ""))
                        break
            except Exception:
                question = str(req)[:200]
            if not question:
                continue
            if isinstance(quality, dict):
                rationale = quality.get("rationale") or ""
                fb = quality.get("feedback") or {}
                value = fb.get("value") if isinstance(fb, dict) else getattr(fb, "value", None)
            else:
                rationale = getattr(quality, "rationale", None) or ""
                value = getattr(quality, "value", None)
                if hasattr(value, "value"):
                    value = value.value  # type: ignore[union-attr]
            cases.append({
                "question": question,
                "expected_judge": rationale or ("response should be correct and grounded" if value else "response should decline or be flagged"),
                "expected_pass": bool(value),
                "status": "pending",
                "response": "",
                "trace_id": str(row.get("trace_id", "")),
            })
        return cases
    except Exception:
        return []


async def _fetch_eval_cases_async(experiment_id: str) -> list[dict[str, Any]]:
    """Run _fetch_eval_cases_sync in the default thread-pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_eval_cases_sync, experiment_id)


def _fetch_traces_list_sync(experiment_id: str | None, max_results: int) -> list[dict[str, Any]]:
    """Fetch recent traces from MLflow (sync, call via executor)."""
    try:
        from mlflow.tracking import MlflowClient as _MlflowClient
        client = _MlflowClient()
        exp_ids: list[str] = (
            [experiment_id] if experiment_id
            else [e.experiment_id for e in client.search_experiments()]
        )
        traces = list(client.search_traces(
            locations=exp_ids,
            max_results=max_results,
            order_by=["timestamp DESC"],
            include_spans=False,
        )) if exp_ids else []
    except Exception:
        logger.exception("mlflow search_traces failed for trace panel")
        traces = []
    traces = _drop_warmup_traces(traces)
    rows = []
    for t in traces:
        info = t.info
        dur_ms = int(info.execution_duration / 1_000_000) if info.execution_duration else None
        # Token cost from trace metadata using published FMAPI rates (#836).
        cost_usd: float | None = None
        try:
            meta = getattr(info, "request_metadata", None) or {}
            token_raw = meta.get("mlflow.trace.tokenUsage")
            if token_raw:
                tu = _json.loads(token_raw)
                priced = usd_for_token_usage(
                    int(tu.get("input_tokens") or 0),
                    int(tu.get("output_tokens") or 0),
                    model="databricks-claude-sonnet-4-6",
                )
                cost_usd = None if priced is None else round(priced, 6)
        except Exception:
            pass
        rows.append({
            "trace_id": info.trace_id,
            "state": info.state.value if hasattr(info.state, "value") else str(info.state),
            "request_time_ms": info.request_time,
            "duration_ms": dur_ms,
            "request_preview": info.request_preview or "",
            "response_preview": info.response_preview or "",
            "cost_usd": cost_usd,
        })
    return rows


async def _fetch_traces_list_async(experiment_id: str | None, max_results: int) -> list[dict[str, Any]]:
    """Run _fetch_traces_list_sync in the default thread-pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_traces_list_sync, experiment_id, max_results)


_LATENCY_TREND_WINDOW = 20


def _latency_trend(rows: list[dict[str, Any]], *, window: int = _LATENCY_TREND_WINDOW) -> dict[str, Any]:
    """p50/p95 + chronological sparkline points from trace-list rows.

    Trace list is newest-first; the polyline needs oldest → newest so a
    left-to-right sparkline tracks latency as instructions change. Rows
    without ``duration_ms`` (ring-buffer-only merges) are dropped.
    """
    from ._eval_sweep import _percentile

    durs: list[int] = []
    for row in rows:
        dur = row.get("duration_ms") if isinstance(row, dict) else None
        if isinstance(dur, bool) or not isinstance(dur, int):
            continue
        durs.append(dur)
        if len(durs) >= window:
            break
    points = list(reversed(durs))
    ranked = sorted(points)
    return {
        "p50_ms": _percentile(ranked, 50),
        "p95_ms": _percentile(ranked, 95),
        "points": points,
        "n": len(points),
    }


def _tool_fail_rates(spans_by_trace: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Aggregate TOOL/RETRIEVER span fail rates from ring-buffer traces.

    Fail matches the list-row ``st-err`` test (``ERR`` / ``FAIL`` substring)
    so serialized ``ERROR`` / ``STATUS_CODE_ERROR`` both count. LLM / CHAIN
    / AGENT spans are ignored because they would drown the strip.
    """
    failed_counts: dict[str, int] = {}
    total_counts: dict[str, int] = {}
    for spans in spans_by_trace:
        if not isinstance(spans, list):
            continue
        for span in spans:
            if not isinstance(span, dict):
                continue
            span_type = span.get("span_type")
            if not isinstance(span_type, str):
                continue
            if _span_type_css(span_type) != "TOOL":
                continue
            name = span.get("name")
            if not isinstance(name, str) or not name:
                continue
            status = span.get("status")
            status_u = status.upper() if isinstance(status, str) else ""
            total_counts[name] = total_counts.get(name, 0) + 1
            if "ERR" in status_u or "FAIL" in status_u:
                failed_counts[name] = failed_counts.get(name, 0) + 1
    tools = [
        {
            "name": name,
            "failed": failed_counts.get(name, 0),
            "total": total,
        }
        for name, total in total_counts.items()
        if total > 0
    ]
    tools.sort(key=lambda row: (-int(row["failed"]), str(row["name"])))
    return {"tools": tools, "n_traces": len(spans_by_trace)}


def _question_from_spans(spans: list[dict[str, Any]]) -> str | None:
    """First user-message content from serialized chat-input spans."""
    for span in spans:
        if not isinstance(span, dict):
            continue
        msgs = _is_chat_messages(span.get("inputs"))
        if msgs is None:
            continue
        for msg in msgs:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content:
                return content
    return None


def _response_from_spans(spans: list[dict[str, Any]]) -> str | None:
    """Last assistant choice content from serialized chat-output spans."""
    for span in reversed(spans):
        if not isinstance(span, dict):
            continue
        choices = _is_choices(span.get("outputs"))
        if choices is None:
            continue
        for choice in reversed(choices):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                return content
    return None


def _eval_row_for_trace(trace_id: str, rows: list[Any]) -> dict[str, Any] | None:
    for row in rows:
        if isinstance(row, dict) and row.get("trace_id") == trace_id:
            return row
    return None


def _trace_diff_side(
    trace_id: str,
    spans: list[dict[str, Any]] | None,
    eval_rows: list[Any],
) -> dict[str, Any]:
    """Build one compare column from the ring buffer + optional evals.json row.

    Never calls MLflow. Missing judge fields stay None so the response model
    omits a fake verdict.
    """
    found = spans is not None
    span_list = spans if isinstance(spans, list) else []
    question = _question_from_spans(span_list)
    response = _response_from_spans(span_list)
    judge_verdict: str | None = None
    judge_reason: str | None = None
    row = _eval_row_for_trace(trace_id, eval_rows)
    if row is not None:
        if question is None:
            cached_q = row.get("question")
            if isinstance(cached_q, str) and cached_q:
                question = cached_q
        if response is None:
            cached_a = row.get("response")
            if isinstance(cached_a, str) and cached_a:
                response = cached_a
        verdict = row.get("judge_verdict")
        if isinstance(verdict, str) and verdict:
            judge_verdict = verdict
        reason = row.get("judge_reason")
        if isinstance(reason, str) and reason:
            judge_reason = reason
    return {
        "trace_id": trace_id,
        "found": found,
        "question": question,
        "response": response,
        "judge_verdict": judge_verdict,
        "judge_reason": judge_reason,
    }


def _render_trace_diff(left: dict[str, Any], right: dict[str, Any]) -> str:
    """Server-rendered two-column compare. Own card class — not data-card."""
    import html as _html

    def _text(value: Any, fallback: str) -> str:
        if isinstance(value, str) and value:
            return _html.escape(value)
        return fallback

    def _card(side: dict[str, Any], label: str) -> str:
        tid = _text(side.get("trace_id"), "—")
        missing = "" if side.get("found") else '<div class="empty">Not in recent buffer</div>'
        verdict = side.get("judge_verdict")
        verdict_cls = "st-ok" if verdict == "PASS" else ("st-err" if isinstance(verdict, str) and verdict else "")
        verdict_html = (
            f'<div class="trace-diff-label">Judge</div>'
            f'<div class="trace-diff-body {verdict_cls}">{_text(verdict, "—")}</div>'
        )
        reason = side.get("judge_reason")
        reason_html = (
            f'<div class="trace-diff-label">Reason</div>'
            f'<div class="trace-diff-body">{_text(reason, "—")}</div>'
            if isinstance(reason, str) and reason
            else ""
        )
        return (
            f'<section class="trace-diff-card">'
            f'<div class="trace-diff-label">{_html.escape(label)}</div>'
            f'<div><a href="/_apx/traces/{tid}">{tid}</a></div>'
            f"{missing}"
            f'<div class="trace-diff-label">Request</div>'
            f'<div class="trace-diff-body">{_text(side.get("question"), "—")}</div>'
            f'<div class="trace-diff-label">Response</div>'
            f'<div class="trace-diff-body">{_text(side.get("response"), "—")}</div>'
            f"{verdict_html}{reason_html}"
            f"</section>"
        )

    left_q = left.get("question") if isinstance(left.get("question"), str) else None
    right_q = right.get("question") if isinstance(right.get("question"), str) else None
    if left_q and right_q and left_q == right_q:
        banner = "Same question — before / after compare"
    elif left_q and right_q:
        banner = "Different questions — compare anyway"
    else:
        banner = "Compare two traces"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Trace diff</title>
{apx_theme_style()}
<style>{_TRACE_CSS}</style>
</head><body>
<header>
  <span class="badge">APX</span><h1>Trace diff</h1>
  <a class="back" href="/_apx/traces">← All traces</a>
</header>
<main>
  <div class="trace-diff-banner">{_html.escape(banner)}</div>
  <div class="trace-diff-grid">
    {_card(left, "Left")}
    {_card(right, "Right")}
  </div>
</main>
</body></html>"""


async def _refresh_eval_cache(experiment_id: str) -> None:
    """Background task: refresh eval cases cache silently."""
    try:
        cases = await _fetch_eval_cases_async(experiment_id)
        if cases:
            _EVAL_CASES_CACHE.put(cases)
        else:
            _EVAL_CASES_CACHE._refreshing = False
    except Exception:
        _EVAL_CASES_CACHE._refreshing = False


async def _refresh_traces_cache(experiment_id: str | None, max_results: int) -> None:
    """Background task: refresh traces list cache silently."""
    try:
        rows = await _fetch_traces_list_async(experiment_id, max_results)
        _TRACES_LIST_CACHE.put(rows)
    except Exception:
        _TRACES_LIST_CACHE._refreshing = False


# ---------------------------------------------------------------------------
# Hub agent crawl helper (mockable for testing)
# ---------------------------------------------------------------------------


async def _crawl_agent(url: str) -> dict | None:
    """Fetch /.well-known/agent.json from a deployed agent. Returns None on failure.

    Validates the URL against SSRF / redirect risks before crawling.
    """
    import httpx as _httpx

    # SSRF guard: validate URL before crawling (host allowlist + DNS check).
    reason = validate_wire_peer_url(url)
    if reason is not None:
        logger.warning("Crawl rejected for %s: %s", url, reason)
        return None

    try:
        async with _httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{url.rstrip('/')}/.well-known/agent.json",
                follow_redirects=False  # Prevent redirects to link-local / private space
            )
            r.raise_for_status()
            card = r.json()
            if isinstance(card, dict):
                # The running agent declares its trace header on the response.
                # Keep it so registration does not invent one.
                header = r.headers.get("traceparent")
                if header:
                    card["trace_header"] = header
            return card
    except Exception:
        return None



def _last_route_payload(
    ctx: AgentContext, *, include_tracking_fallback: bool = True
) -> dict[str, Any]:
    tid, spans = _latest_buffered_spans()
    if include_tracking_fallback and not spans:
        # Fall back to tracking-store metadata + buffer/detail fetch for the newest id.
        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID")
        try:
            from mlflow.tracking import MlflowClient as _MlflowClient

            client = _MlflowClient()
            exp_ids: list[str] = (
                [experiment_id] if experiment_id
                else [e.experiment_id for e in client.search_experiments()]
            )
            traces = list(client.search_traces(
                locations=exp_ids,
                max_results=5,
                order_by=["timestamp DESC"],
                include_spans=False,
            )) if exp_ids else []
            traces = _drop_warmup_traces(traces)
            for t in traces:
                candidate = getattr(getattr(t, "info", None), "trace_id", None)
                if not candidate:
                    continue
                buffered = None
                try:
                    from ._trace_store import get as _ts_get

                    buffered = _ts_get(candidate)
                except Exception:
                    buffered = None
                if buffered:
                    tid, spans = candidate, buffered
                    break
        except Exception:
            logger.debug("last-route mlflow search failed", exc_info=True)

    if not spans:
        return {
            "trace_id": tid,
            "node_ids": [],
            "edge_ids": [],
            "tool_names": [],
            "span_count": 0,
        }

    topo = build_topology(ctx)
    highlight = route_highlight_from_spans(topo, spans)
    return {
        "trace_id": tid,
        "node_ids": highlight["node_ids"],
        "edge_ids": highlight["edge_ids"],
        "tool_names": highlight["tool_names"],
        "span_count": len(spans),
    }

# Static bundles the topology and discover pages share. Computed from this
# module so route families can move without the path changing.
_topo_static_root = Path(__file__).parent / "_static" / "topology"
_vendor_root = Path(__file__).parent / "_static" / "vendor"


def build_dev_ui_router(api_prefix: str = "/api") -> APIRouter:
    """Build the /_apx/* dev UI routes.

    Note: ``/`` is owned by the end-user chat (see ``_ui_root_chat``), not the
    dev console. The dev console's entry point is ``/_apx/agent``.

    Routes live in ``_dev_routes`` by family. This function only builds the
    guarded router and the small context those families close over.
    """
    from ._dev_routes import mount
    from ._dev_routes.context import DevRouteContext

    # Router-level guard: enforces auth on write methods + the SSRF probe.
    # See _dev_write_guard / _enforce_dev_write_auth for the posture (H17/H18).
    router = APIRouter(dependencies=[Depends(_dev_write_guard)])
    # Save changes the file without reloading the running graph. Scores must
    # refer to the source generation present when these routes were mounted.
    instruction_source_path = _find_agent_router_path()
    instruction_source = (
        instruction_source_path.read_text()
        if instruction_source_path and instruction_source_path.is_file()
        else None
    )
    mount(
        router,
        DevRouteContext(
            api_prefix=api_prefix,
            instruction_source_path=instruction_source_path,
            instruction_source=instruction_source,
        ),
    )
    return router
