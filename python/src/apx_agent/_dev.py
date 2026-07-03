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
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

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
    TraceDetailResponse,
    TraceRow,
    VsIndexInfo,
    WarehouseInfo,
    WireAgentRequest,
    WireAgentResponse,
    WorkspaceContextResponse,
)
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
from ._ui_probe import _generate_agent_instructions, _render_probe_ui, _run_probe_checks, _discover_vs_indexes, _validate_probe_url

logger = logging.getLogger(__name__)


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
  /* Compact, deduped conversation view (chat payloads) */
  .convo{display:flex;flex-direction:column;gap:5px;}
  .tmsg{display:flex;gap:8px;font-size:12px;line-height:1.45;align-items:baseline;}
  .trole{flex:none;min-width:74px;color:#6b7686;font-size:10px;font-weight:600;
         text-transform:uppercase;letter-spacing:.4px;font-family:ui-monospace,monospace;}
  .trole-tool{color:#4a9060;}
  .tcontent{color:#cbd2da;white-space:pre-wrap;word-break:break-word;
            font-family:ui-monospace,monospace;font-size:11.5px;}
  details.tsys{font-size:12px;}
  details.tsys>summary,details.tprefix>summary{cursor:pointer;color:#6b7686;font-size:11px;
            list-style:none;padding:1px 0;}
  details.tsys>summary::-webkit-details-marker,
  details.tprefix>summary::-webkit-details-marker{display:none;}
  details.tsys>summary::before,details.tprefix>summary::before{content:"▸ ";color:#4b5563;}
  details.tsys[open]>summary::before,details.tprefix[open]>summary::before{content:"▾ ";}
  details.tprefix{border-left:2px solid #1e1e1e;padding-left:8px;margin:2px 0;}
  details.tprefix[open]{display:flex;flex-direction:column;gap:5px;}
  pre.tpre{background:#0d0d0d;border:1px solid #1e1e1e;border-radius:5px;margin:4px 0 0;
           padding:8px 10px;font-size:11px;font-family:monospace;color:#9aa3ad;
           white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto;}
  .span-event{font-size:11px;color:#888;padding:3px 14px 3px 30px;
              border-top:1px solid #161616;font-family:monospace;
              white-space:pre-wrap;word-break:break-word;}
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
    import html as _html

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
                out = _json.dumps(cleaned, indent=2)
                io_html += f'<div class="io-block"><div class="io-label">Outputs</div><pre class="io-pre">{_html.escape(out[:4000])}</pre></div>'
            # Progress markers (span events) render in an always-visible strip
            # so a cold-start step shows even while the body is collapsed.
            events_html = ""
            for ev in (s.get("events") or []):
                msg = (ev.get("attributes") or {}).get("message") or ev.get("name", "")
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
# Dev-UI write authorization (audit H17/H18)
#
# The dev router hosts code-write/RCE-equivalent endpoints (POST /_apx/edit,
# /_apx/tools/new, /_apx/tools/suggest, /_apx/replay/*, save_setup, env writes,
# DELETE /_apx/tools/{name}) and an SSRF probe (/_apx/setup/probe-json). Those
# write/probe endpoints must not be reachable by every authorized App viewer.
#
# Posture:
#   * Local ``apx-agent run`` (no DATABRICKS_APP_PORT) → writes allowed (the dev loop).
#   * Deployed Databricks App (DATABRICKS_APP_PORT set):
#       - APX_DEV_UI_TOKEN unset  → writes DENIED (safe default).
#       - APX_DEV_UI_TOKEN set    → require a matching ``X-APX-Dev-Token`` header
#                                   (or ``token`` query param); 403 otherwise.
#
# A dedicated header is used rather than Authorization/Bearer so it never
# collides with the platform's ``Authorization`` / ``X-Forwarded-Access-Token``.
# The guard is attached once at the router level and only enforces on
# state-changing methods plus the SSRF probe, so read GETs stay open.
# ---------------------------------------------------------------------------

_DEV_TOKEN_HEADER = "x-apx-dev-token"
_DEV_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_deployed_app() -> bool:
    """True when running inside a deployed Databricks App.

    Env-only (``DATABRICKS_APP_PORT``) deliberately — a local ``apx-agent run`` that
    happens to sit behind a proxy must not be locked out, so the X-Forwarded-*
    heuristic used elsewhere is not consulted here.
    """
    return bool(os.environ.get("DATABRICKS_APP_PORT"))


def _enforce_dev_write_auth(request: Request) -> None:
    """Authorize a dev-UI write/probe request, or raise 403.

    See the module-level posture comment for the full decision table.
    """
    import hmac

    raw = os.environ.get("APX_DEV_UI_TOKEN")
    token = raw.strip() if raw else ""

    if not token:
        # No token configured. Allow locally; deny on a deployed App so the
        # code-write/probe surface is never open to authorized App viewers.
        if _is_deployed_app():
            raise HTTPException(
                status_code=403,
                detail=(
                    "Dev-UI write endpoints are disabled on deployed Apps. "
                    "Set the APX_DEV_UI_TOKEN env var and send it as the "
                    "X-APX-Dev-Token header to enable them."
                ),
            )
        return

    supplied = request.headers.get(_DEV_TOKEN_HEADER) or request.query_params.get("token") or ""
    if not (supplied and hmac.compare_digest(supplied, token)):
        raise HTTPException(
            status_code=403,
            detail="Missing or invalid X-APX-Dev-Token for dev-UI write endpoint.",
        )


async def _dev_write_guard(request: Request) -> None:
    """Router-level dependency: gate state-changing methods + the SSRF probe.

    Attached at router construction so it covers every current and future
    write route (edit, tools/new, tools/suggest, replay/*, setup writes,
    DELETE tools) without per-route annotations that could miss one. Read GETs
    (chat, traces, topology, probe/checks, setup/catalogs, …) fall through.
    """
    # Two side-effecting GETs need gating too: the SSRF probe and
    # /_apx/deploy/stream, which spawns ``apx-agent deploy`` as a subprocess
    # (privileged, state-changing — same threat class as the write endpoints).
    path = request.url.path
    is_side_effecting_get = path.endswith("/setup/probe-json") or path.endswith(
        "/deploy/stream"
    )
    if request.method in _DEV_WRITE_METHODS or is_side_effecting_get:
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


def build_dev_ui_router(api_prefix: str = "/api") -> APIRouter:
    """Build the /_apx/* dev UI routes.

    Note: ``/`` is owned by the end-user chat (see ``_ui_root_chat``), not the
    dev console. The dev console's entry point is ``/_apx/agent``.
    """
    # Router-level guard: enforces auth on write methods + the SSRF probe.
    # See _dev_write_guard / _enforce_dev_write_auth for the posture (H17/H18).
    router = APIRouter(dependencies=[Depends(_dev_write_guard)])

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

    @router.get("/_apx/probe", include_in_schema=False)
    async def probe_dev_ui() -> HTMLResponse:
        return HTMLResponse(_render_probe_ui())

    @router.get("/_apx/probe/checks", response_model=dict[str, Any])
    async def probe_checks(request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        conversation_store = getattr(request.app.state, "conversation_store", None)
        return await _run_probe_checks(
            ctx,
            headers=dict(request.headers),
            conversation_store=conversation_store,
        )

    @router.get("/_apx/conversations",
                response_model=list[ConversationSummaryResponse])
    async def list_conversations_api(request: Request) -> Any:
        """Return conversations from the conversation store as JSON."""
        store = getattr(request.app.state, "conversation_store", None)
        if store is None:
            return []
        ctx: AgentContext | None = getattr(request.app.state, "agent_context", None)
        agent_id = ctx.config.name if ctx else None
        try:
            paged = store.list_conversations(
                limit=50,
                order="desc",
                sort_by="updated_at",
                agent_id=agent_id,
            )
            # Raw list (not JSONResponse) so ``response_model`` actually
            # validates each row — FastAPI passes a Response through untouched.
            return [
                {
                    "id": c.id,
                    "title": c.title,
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                }
                for c in paged.data
            ]
        except Exception:
            # Surface the failure to the UI (history panel shows an error
            # state) instead of silently rendering an empty list, and log
            # it so `apx-agent run` output points at the real cause.
            logger.exception("listing conversations failed")
            return JSONResponse({"error": "conversation store unavailable"},
                                status_code=503)

    @router.get("/_apx/conversations/{conv_id}/items",
                response_model=list[ConversationItemResponse])
    async def list_conversation_items_api(conv_id: str, request: Request) -> Any:
        """Return items for a conversation as JSON."""
        store = getattr(request.app.state, "conversation_store", None)
        if store is None:
            return []
        try:
            paged = store.list_items(conv_id, limit=200, order="asc")
            # Raw list (not JSONResponse) so ``response_model`` validates rows.
            return [
                {
                    "id": item.id,
                    "type": item.type,
                    "data": item.data.model_dump(exclude_none=True) if item.data else {},
                }
                for item in paged.data
            ]
        except Exception:
            logger.exception("listing items for conversation %s failed", conv_id)
            return JSONResponse({"error": "conversation store unavailable"},
                                status_code=503)

    # ── Approvals (human-in-the-loop ASK policy) ──────────────────────────

    def _find_approval_store(request: Request) -> Any:
        """Locate the ApprovalStore serving this app, if any.

        Resolution order:

        1. ``app.state.approval_store`` — explicit wiring by the embedding
           application (takes precedence so multi-agent apps can share one
           store).
        2. The root agent's ``before_tool`` hook: a bare
           :class:`~apx_agent._policy.PolicyGate` exposes ``.approvals``;
           a :func:`~apx_agent._guards.compose`-d hook exposes
           ``.callbacks`` which is walked for a gate.

        :param request: The incoming request (used for ``app.state``).
        :returns: The :class:`~apx_agent._policy.ApprovalStore`, or
            ``None`` when no PolicyGate is attached to this agent.
        """
        explicit = getattr(request.app.state, "approval_store", None)
        if explicit is not None:
            return explicit
        ctx: AgentContext | None = getattr(request.app.state, "agent_context", None)
        agent = getattr(ctx, "agent", None) if ctx else None
        hook = getattr(agent, "_before_tool", None)
        candidates = [hook] if hook is not None else []
        # compose() exposes .callbacks — one level of nesting is all the
        # wiring layer produces (compose(guard, gate)), so no recursion.
        candidates.extend(getattr(hook, "callbacks", []) or [])
        for cand in candidates:
            approvals = getattr(cand, "approvals", None)
            if approvals is not None:
                return approvals
        return None

    @router.get("/_apx/approvals", response_model=list[ApprovalInfo])
    async def list_approvals_api(request: Request) -> Any:
        """Return pending approval requests as JSON for the chat UI banner."""
        from fastapi.responses import JSONResponse
        store = _find_approval_store(request)
        if store is None:
            return []
        try:
            return [
                {
                    "id": a.id,
                    "tool_name": a.tool_name,
                    "arguments": a.arguments,
                    "reason": a.reason,
                }
                for a in store.list_pending()
            ]
        except Exception:
            logger.exception("listing approvals failed")
            return JSONResponse({"error": "approval store unavailable"},
                                status_code=503)

    @router.post("/_apx/approvals/{approval_id}/approve", response_model=ApprovalActionResponse)
    async def approve_approval_api(approval_id: str, request: Request) -> Any:
        """Grant a pending approval — the agent's retry of the call passes."""
        from fastapi.responses import JSONResponse
        store = _find_approval_store(request)
        if store is None:
            return JSONResponse({"error": "no approval store configured"},
                                status_code=404)
        try:
            approval = store.approve(approval_id)
        except KeyError:
            return JSONResponse({"error": f"unknown approval {approval_id}"},
                                status_code=404)
        return {"id": approval.id, "status": approval.status}

    @router.post("/_apx/approvals/{approval_id}/deny", response_model=ApprovalActionResponse)
    async def deny_approval_api(approval_id: str, request: Request) -> Any:
        """Refuse a pending approval — the agent's retry becomes a hard DENY."""
        from fastapi.responses import JSONResponse
        store = _find_approval_store(request)
        if store is None:
            return JSONResponse({"error": "no approval store configured"},
                                status_code=404)
        try:
            approval = store.deny(approval_id)
        except KeyError:
            return JSONResponse({"error": f"unknown approval {approval_id}"},
                                status_code=404)
        return {"id": approval.id, "status": approval.status}

    @router.get("/_apx/traces", response_model=list[TraceRow])
    async def traces_list_ui(request: Request) -> Any:
        import os
        ctx: AgentContext | None = getattr(request.app.state, "agent_context", None)
        agent_name = ctx.config.name if ctx else None
        fmt = request.query_params.get("fmt")
        max_results = int(request.query_params.get("max", "50"))
        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID")
        try:
            # include_spans=False skips artifact download — works even when
            # the blob-storage endpoint is unreachable (e.g. private-link
            # workspaces where *.storage.cloud.databricks.com is blocked).
            from mlflow.tracking import MlflowClient as _MlflowClient
            client = _MlflowClient()
            # MLflow's search_traces(experiment_ids=None) trips on the local
            # sqlite store ("'NoneType' object is not iterable"), which is the
            # default backend for local `apx-agent run` — so the Trace panel sees
            # nothing even when traces are being recorded. Resolve to all
            # experiments when no MLFLOW_EXPERIMENT_ID is set so the dev loop
            # surfaces its traces. In the deployed runtime MLFLOW_EXPERIMENT_ID
            # is always set and this branch is a no-op.
            exp_ids: list[str] = (
                [experiment_id] if experiment_id
                else [e.experiment_id for e in client.search_experiments()]
            )
            traces = list(client.search_traces(
                locations=exp_ids,
                max_results=max_results,
                order_by=["timestamp DESC"],
                include_spans=False,
                flush=True,  # MLflow 3.x writes async; flush before search
            )) if exp_ids else []
        except Exception:
            # Keep the panel functional (ring-buffer merge below still
            # surfaces recent traces) but log why the tracking store
            # search failed instead of hiding it.
            logger.exception("mlflow search_traces failed for trace panel")
            traces = []
        traces = _drop_warmup_traces(traces)
        rows = []
        seen_ids: set[str] = set()
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
            seen_ids.add(info.trace_id)
        if fmt == "json":
            # Merge in any ring-buffer traces not yet committed to the tracking
            # store (async write lag, or FEVM blob-egress blocked). Newest first.
            try:
                from ._trace_store import list_recent as _ts_list_recent
                for tid in _ts_list_recent(max_results):
                    if tid not in seen_ids:
                        rows.insert(0, {
                            "trace_id": tid,
                            "state": "OK",
                            "request_time_ms": None,
                            "duration_ms": None,
                            "request_preview": "",
                            "response_preview": "",
                        })
                        seen_ids.add(tid)
            except Exception:
                pass
            rows = rows[:max_results]
            return rows
        return HTMLResponse(_render_traces_list(rows, agent_name))

    @router.get("/_apx/traces/{trace_id:path}", response_model=TraceDetailResponse)
    async def trace_detail_ui(trace_id: str, request: Request) -> Any:
        from fastapi.responses import JSONResponse
        from ._trace_store import get as _ts_get, put as _ts_put

        fmt = request.query_params.get("fmt")

        # 1) Buffer hit — serve from the in-process ring buffer (captured at
        #    root-span-end, no blob fetch). This is the FEVM/private-link path:
        #    recent traces are served from memory and never touch get_trace.
        buffered = _ts_get(trace_id)
        if buffered is not None:
            if fmt == "json":
                return {"trace_id": trace_id, "spans": buffered}
            return HTMLResponse(_render_trace_detail(trace_id, buffered, None))

        # 2) Buffer miss — fall through to mlflow.get_trace under a worker-thread
        #    TIMEOUT so a blocked blob fetch fails fast instead of hanging. The
        #    executor is NOT used as a context manager: its __exit__ would
        #    shutdown(wait=True) and re-join the still-hung worker, undoing the
        #    timeout. On timeout we abandon the orphaned thread.
        import concurrent.futures as _futures

        _GET_TRACE_TIMEOUT_S = 5.0
        unavailable = (
            "span data unavailable on this workspace (artifact-storage egress "
            "blocked) — recent traces are served from memory"
        )

        def _fetch() -> Any:
            import mlflow as _mlflow
            return _mlflow.get_trace(trace_id)

        executor = _futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = executor.submit(_fetch)
            try:
                trace = fut.result(timeout=_GET_TRACE_TIMEOUT_S)
            except _futures.TimeoutError:
                executor.shutdown(wait=False, cancel_futures=True)
                if fmt == "json":
                    return JSONResponse({"error": unavailable}, status_code=200)
                return HTMLResponse(_render_trace_detail(trace_id, None, unavailable))
            except Exception as exc:
                executor.shutdown(wait=False, cancel_futures=True)
                if fmt == "json":
                    return JSONResponse({"error": str(exc)}, status_code=404)
                return HTMLResponse(_render_trace_detail(trace_id, None, str(exc)))
        finally:
            # Non-blocking shutdown — never wait on a possibly-hung worker.
            executor.shutdown(wait=False)

        if trace is None:
            if fmt == "json":
                return JSONResponse({"error": "not found"}, status_code=404)
            return HTMLResponse(_render_trace_detail(trace_id, None, "Trace not found"))

        # 3) Success — serve AND opportunistically populate the buffer so the
        #    next view (and the FEVM path) hits memory.
        span_dicts = _serialize_trace_spans(trace)
        if span_dicts:
            _ts_put(trace_id, span_dicts)
        if fmt == "json":
            return {"trace_id": trace_id, "spans": span_dicts}
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

    @router.get("/_apx/topology.json", response_model=TopologyResponse)
    async def topology_json(request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse(
                {"error": "Agent context not available"}, status_code=503
            )
        return build_topology(ctx)

    @router.get("/_apx/topology/inspect/{node_id:path}", response_model=dict[str, Any])
    async def topology_inspect(node_id: str, request: Request) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse(
                {"error": "Agent context not available"}, status_code=503
            )
        details = inspect_node(ctx, node_id)
        if details is None:
            raise HTTPException(status_code=404, detail=f"Node not found: {node_id}")
        return details

    @router.get("/_apx/topology/assets/{path:path}", include_in_schema=False)
    async def topology_assets(path: str) -> Any:
        target = _topo_static_root / "assets" / path
        if target.is_file():
            return FileResponse(target)
        raise HTTPException(status_code=404, detail="asset not found")

    # Vendored JS (marked + DOMPurify) for chat markdown rendering. Served
    # locally so the deployed app needs no CDN (offline/private-link safe).
    _vendor_root = _TopoPath(__file__).parent / "_static" / "vendor"

    @router.get("/_apx/vendor/{filename}", include_in_schema=False)
    async def vendor_asset(filename: str) -> Any:
        # Resolve and confirm the result is inside _vendor_root (block path
        # traversal). {filename} is a single path segment, so a traversal
        # attempt also simply fails to match this route — guard is belt-and-braces.
        target = (_vendor_root / filename).resolve()
        if _vendor_root.resolve() not in target.parents or not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(target, media_type="application/javascript")

    @router.post("/_apx/replay/tool", include_in_schema=False)
    async def replay_tool(request: Request, body: ReplayToolRequest) -> Any:
        """Re-invoke a registered tool with arbitrary args. Used by the
        trace detail view to debug-iterate without restarting a conversation.

        Kept ``include_in_schema=False`` (#281): executes an arbitrary tool with
        forwarded OBO creds — the body is typed/validated but never advertised."""
        from fastapi.responses import JSONResponse
        import time as _time
        from httpx import ASGITransport, AsyncClient

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        tool_name = body.tool_name
        args = body.args
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
    async def replay_llm(request: Request, body: ReplayLlmRequest) -> Any:
        """Re-invoke the configured model with edited messages. Returns
        the model's output text — useful for "what if I had asked X instead?".

        Kept ``include_in_schema=False`` (#281): invokes the LLM directly — the
        body is typed/validated but never advertised."""
        from fastapi.responses import JSONResponse
        import time as _time

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        messages = body.messages
        model = body.model or getattr(ctx.config, "model", "")
        if not model:
            return JSONResponse({"ok": False, "error": "No model configured"}, status_code=400)

        try:
            from databricks_openai import AsyncDatabricksOpenAI
        except ImportError as exc:
            return JSONResponse({"ok": False, "error": f"databricks_openai not available: {exc}"}, status_code=500)

        t0 = _time.monotonic()
        try:
            client = AsyncDatabricksOpenAI()
            resp = await client.chat.completions.create(
                model=model, messages=messages,  # type: ignore[arg-type]  # validated dicts; SDK wants ChatCompletionMessageParam
            )
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

    @router.get("/_apx/edit")
    async def edit_dev_ui(request: Request) -> HTMLResponse:
        path = _find_agent_router_path()
        if path and path.exists():
            return HTMLResponse(_render_edit_ui(path.read_text()))
        # No agent.py on disk (config-only / template agent). Synthesize a
        # read-only ADK-style view from the live agent config + live tool schemas
        # so the Edit page shows the agent instead of an empty "not found" page.
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is not None and getattr(ctx.config, "template", None):
            from ._project_gen import render_agent_py
            try:
                code = render_agent_py(ctx.config)
            except Exception:  # noqa: BLE001 — fall back to the not-found page
                code = ""
            if code:
                schemas = [
                    {"name": t.name, "description": t.description,
                     "parameters": t.input_schema or {}}
                    for t in (ctx.tools or [])
                ]
                return HTMLResponse(
                    _render_edit_ui(code, read_only=True, initial_schemas=schemas)
                )
        return HTMLResponse(_render_edit_ui("", not_found=True))

    @router.post("/_apx/edit", response_model=EditSaveResponse)
    async def save_agent_router(request: Request, body: EditSaveRequest) -> Any:
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse
        content: str = body.content
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

        return {"ok": True, "restart_required": restart_required}

    @router.post("/_apx/edit/preview", response_model=list[dict[str, Any]])
    async def preview_tool_schemas(body: EditPreviewRequest) -> Any:
        return _extract_schemas_from_source(body.source)

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
        from ._ui_edit import _parse_agent_nodes
        nodes = _parse_agent_nodes(updated)
        wired = any(name in (n.get("tools") or []) for n in nodes)
        if not wired:
            leaves = [n["name"] for n in nodes if n["name"] != "agent" and n.get("wrapper") is None]
            return {
                "ok": True, "wired": False, "agents": leaves,
                "note": (
                    f"`{name}` was added to agent.py but not attached to an agent "
                    f"(this agent is composed). Re-add it choosing one of: {', '.join(leaves) or '(define a leaf agent first)'}."
                ),
            }
        return {"ok": True, "wired": True}

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
            yield f"data: Running: apx-agent deploy {root}\n\n"
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

        # Backfill any missing catalog/schema/warehouse with a workspace probe so a
        # fresh agent's Setup page lands pre-populated with the most relevant items
        # from the user's workspace, rather than three "Loading…" dropdowns the
        # user has to scan manually. .env values always win (user-curated wins).
        if not (current.get("DEMO_CATALOG")
                and current.get("DEMO_SCHEMA")
                and current.get("WAREHOUSE_ID")):
            try:
                ws: WorkspaceClient = request.app.state.workspace_client
                picked = await asyncio.to_thread(_pick_workspace_defaults, ws)
                for k, v in picked.items():
                    current.setdefault(k, v)
            except Exception:
                # Best-effort — the page still works with empty defaults, the user
                # just picks manually like before. No crash on probe failure.
                pass

        return HTMLResponse(_render_setup_ui(current, embed=embed))

    @router.get("/_apx/workspace-context", response_model=WorkspaceContextResponse)
    async def workspace_context(request: Request) -> Any:
        """Return workspace identity + agent resource summary for the Context tab."""
        import asyncio as _asyncio

        ws: WorkspaceClient = request.app.state.workspace_client
        ctx = request.app.state.agent_context

        # Workspace identity
        host = (getattr(getattr(ws, "config", None), "host", None) or "").rstrip("/")
        try:
            me = await _asyncio.to_thread(ws.current_user.me)
            user_name = getattr(me, "user_name", None) or getattr(me, "display_name", None) or "unknown"
        except Exception:
            user_name = "unknown"

        # Agent declared resources
        resources: list[dict] = []
        if ctx is not None:
            try:
                from ._resources import collect_resource_specs
                for spec in collect_resource_specs(ctx.agent):
                    resources.append({"kind": spec.kind, "identifier": spec.identifier})
            except Exception:
                pass

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
            "resources": resources,
            "used_catalogs": used_catalogs,
            "used_schemas": used_schemas,
        }

    @router.get("/_apx/grounding")
    async def grounding_ui() -> Any:
        """The field-description curation page (#292) — review + accept/edit
        per-column descriptions for the agent's OKF grounding bundle."""
        from ._ui_grounding import render_grounding_ui

        return HTMLResponse(render_grounding_ui())

    @router.get("/_apx/grounding/columns", response_model=GroundingColumnsResponse)
    async def grounding_columns(request: Request) -> Any:
        """Per-column current-vs-suggested description curation state for the
        agent's OKF bundle (#292). Suggestions come from Unity Catalog COMMENTs;
        the response has empty ``tables`` when the project has no OKF bundle."""
        import asyncio as _asyncio
        from ._ui_grounding import build_column_curation, resolve_okf_root

        okf_root = resolve_okf_root()
        if okf_root is None:
            return {"catalog": "", "schema": "", "tables": []}
        ws = getattr(request.app.state, "workspace_client", None)
        return await _asyncio.to_thread(build_column_curation, okf_root, ws)

    @router.post("/_apx/grounding/columns", response_model=ColumnDescriptionsSaveResponse)
    async def save_grounding_columns(
        request: Request, body: ColumnDescriptionsRequest
    ) -> Any:
        """Write accepted column descriptions into the OKF bundle (#292). Edits
        the LOCAL bundle only — never Unity Catalog. Token-gated by the dev
        write guard like every other write route."""
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse
        from ._ui_grounding import apply_column_descriptions, resolve_okf_root

        okf_root = resolve_okf_root()
        if okf_root is None:
            return JSONResponse({"ok": False, "error": "No .apx/okf bundle found"}, status_code=404)
        modified = await _asyncio.to_thread(apply_column_descriptions, okf_root, body.accepted)
        return {"ok": True, "modified": modified}

    @router.post("/_apx/grounding/suggest", response_model=ColumnSuggestResponse)
    async def suggest_grounding_descriptions(
        request: Request, body: ColumnSuggestRequest
    ) -> Any:
        """LLM-generate column descriptions for a table (#292 phase C) — an AI
        suggestion source alongside UC comments. Reads nothing from / writes
        nothing to the bundle; the UI offers the result for accept like any
        other suggestion."""
        from fastapi.responses import JSONResponse
        from ._okf import okf_columns
        from ._ui_grounding import generate_column_descriptions, resolve_okf_root

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
            from ._memory import MemoryFilter
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

    @router.get("/_apx/setup/catalogs", response_model=list[str])
    async def setup_catalogs(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        ws: WorkspaceClient = request.app.state.workspace_client
        try:
            cats = [c.name for c in ws.catalogs.list() if c.name]
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return sorted(cats)

    @router.get("/_apx/setup/schemas", response_model=list[str])
    async def setup_schemas(request: Request) -> Any:
        from fastapi.responses import JSONResponse
        catalog = request.query_params.get("catalog", "")
        if not catalog:
            return []
        ws: WorkspaceClient = request.app.state.workspace_client
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
        catalog = request.query_params.get("catalog", "")
        schema = request.query_params.get("schema", "")
        if not catalog or not schema:
            return []
        ws: WorkspaceClient = request.app.state.workspace_client
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
        ws: WorkspaceClient = request.app.state.workspace_client
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

        from ._ui_edit import _parse_agent_nodes, _set_agent_instructions, _set_agent_tools

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

        resp: dict[str, Any] = {"ok": True, "applied": applied}
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
        ws: WorkspaceClient = request.app.state.workspace_client
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
        from ._ui_edit import _compose_agents

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
            "ok": True, "type": pattern,
            "agents": [leaf["name"] for leaf in leaves],
            "changed": updated != source,
        }

    @router.get("/_apx/eval/data", response_model=list[EvalCaseResponse])
    async def eval_data_get() -> Any:
        """Read persisted eval cases. Returns [] if no file or no agent_router.

        Success returns the persisted JSON list via ``JSONResponse`` so the
        bytes on the wire are the file verbatim (cases diverge — see
        :class:`~apx_agent._apx_models.EvalCaseResponse`); ``response_model``
        documents the row shape in the OpenAPI schema without filtering the
        response. The parse-error path returns ``{ok: false, error}`` with 500.
        """
        from fastapi.responses import JSONResponse
        path = _find_evals_path()
        if path is None or not path.exists():
            return JSONResponse([])
        try:
            return JSONResponse(_json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/_apx/eval/data", response_model=EvalDataSaveResponse)
    async def eval_data_post(request: Request, cases: list[EvalCaseIn]) -> Any:
        """Replace persisted eval cases with the request body (a list).

        ``cases: list[EvalCaseIn]`` makes FastAPI validate the body shape: a
        non-list, or an element missing ``question``, now yields ``422`` (this
        replaces the old manual ``isinstance``/400 check). The persisted bytes
        come from re-reading the **raw** body — ``EvalCaseIn`` uses
        ``extra="ignore"``, so dumping the parsed models would drop the UI's
        divergent fields; the cached raw body preserves them verbatim. The
        semantic 503 (``agent_router.py`` not found) and 500 (OS write error)
        stay in the handler as ``JSONResponse`` (bypassing ``response_model``).
        """
        from fastapi.responses import JSONResponse
        # Body already validated as a list[EvalCaseIn]; re-read the cached raw
        # body so unmodelled, divergent case fields persist unchanged.
        body = await request.json()
        path = _find_evals_path()
        if path is None:
            return JSONResponse({"ok": False, "error": "agent_router.py not found in running process"}, status_code=503)
        content = _json.dumps(body, indent=2)
        try:
            path.write_text(content)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        await _ws_upload_agent_file(request, path, content)
        # Raw dict (not JSONResponse) so ``response_model`` validates the shape.
        return {"ok": True, "count": len(cases)}

    @router.post("/_apx/eval/judge", response_model=JudgeResponse)
    async def eval_judge(request: Request, judge: JudgeRequest) -> Any:
        """LLM-as-judge scoring for eval cases.

        Body: {question, response, criterion, model?}. A missing required key or
        a wrong type now yields ``422`` via :class:`JudgeRequest`; a present-but-
        blank required field yields ``422`` here in the handler. The judge prompt
        asks the model to reply with PASS/FAIL + a one-sentence reason; we parse
        the verdict deterministically and return {ok, pass, verdict, reason}.
        The semantic 503 (no agent context) is preserved.
        """
        from fastapi.responses import JSONResponse
        import time as _time

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        question = judge.question.strip()
        response = judge.response.strip()
        criterion = judge.criterion.strip()
        if not (question and response and criterion):
            return JSONResponse(
                {"ok": False, "error": "question, response, and criterion are all required"},
                status_code=422,
            )
        model = judge.model or getattr(ctx.config, "model", "")
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
            _judge = _parse_judge_output(text)
            # Raw dict (not JSONResponse) so ``response_model`` validates the
            # JudgeResponse shape; ``pass`` serialises via its field alias.
            return {
                "ok": True,
                "pass": _judge.verdict == "PASS",
                "verdict": _judge.verdict,
                "reason": _judge.reason,
                "duration_ms": elapsed,
                "model": model,
            }
        except Exception as exc:  # noqa: BLE001
            elapsed = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({
                "ok": False,
                "error": str(exc),
                "duration_ms": elapsed,
            }, status_code=200)

    # Redirects for old routes
    @router.get("/_apx/eval", response_class=HTMLResponse)
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

    return router
