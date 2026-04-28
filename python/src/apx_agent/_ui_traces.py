"""Dev UI — /_apx/traces list and detail HTML rendering.

Python parity for typescript/src/dev/index.ts trace UI. Reads from the
in-memory ring buffer in _trace.py.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any

from ._trace import Trace, TraceSpan


def _truncate(value: Any, max_len: int = 120) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    if not s:
        return ""
    return s[:max_len] + "..." if len(s) > max_len else s


def _status_badge(status: str | None) -> str:
    colors = {"in_progress": "#f0ad4e", "completed": "#5cb85c", "error": "#d9534f"}
    color = colors.get(status or "", "#888")
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'background:{color};color:#fff;font-size:0.75rem;font-weight:600;">'
        f'{escape(status or "unknown")}</span>'
    )


def _extract_message(value: Any) -> str:
    """Pull a human-readable message out of a span input/output."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            return _extract_message(json.loads(value))
        except (ValueError, TypeError):
            return value[:500]
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in reversed(value):
            if isinstance(item, dict) and "content" in item:
                return _extract_message(item["content"])
        joined = ", ".join(_extract_message(v) for v in value if v).strip(", ")
        return joined[:300]
    if isinstance(value, dict):
        for key in ("content", "text", "output_text", "message"):
            if key in value:
                return _extract_message(value[key])
        parts = []
        for k, v in value.items():
            if v is None or v == "":
                continue
            if isinstance(v, (int, float)):
                parts.append(f"{k}: {v}")
            elif isinstance(v, str):
                parts.append(f"{k}: {v[:60]}..." if len(v) > 60 else f"{k}: {v}")
            elif isinstance(v, list):
                parts.append(f"{k}: [{len(v)} items]")
            else:
                parts.append(f"{k}: {json.dumps(v, default=str)[:40]}")
        return "\n".join(parts)
    return str(value)[:300]


def _render_traces_list_ui(traces: list[Trace], base_path: str = "") -> str:
    total = len(traces)
    in_progress = sum(1 for t in traces if t.status == "in_progress")
    completed = sum(1 for t in traces if t.status == "completed")
    errored = sum(1 for t in traces if t.status == "error")

    rows = []
    for t in traces:
        first_input = next((s for s in t.spans if s.type == "request"), None)
        input_preview = _truncate(first_input.input, 80) if first_input else ""
        duration = f"{int(t.duration_ms)}ms" if t.duration_ms is not None else "running"
        rows.append(
            f'<tr onclick="location.href=\'{base_path}/_apx/traces/{escape(t.id)}\'" style="cursor:pointer;">'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;font-family:monospace;font-size:0.8rem;">{escape(t.id)}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;">{escape(t.agent_name)}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;">{_status_badge(t.status)}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;text-align:center;">{len(t.spans)}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;text-align:right;font-family:monospace;">{duration}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #333;font-size:0.85rem;color:#aaa;'
            f'max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{escape(input_preview)}</td>'
            f"</tr>"
        )

    rows_html = "\n".join(rows) or (
        '<tr><td colspan="6" style="padding:2rem;text-align:center;color:#666;">No traces yet</td></tr>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="10">
  <title>Agent Traces</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0; min-height: 100vh; }}
    header {{ padding: 1rem; background: #16213e; border-bottom: 1px solid #333; }}
    header h1 {{ font-size: 1.1rem; font-weight: 600; }}
    nav {{ padding: 0.5rem 1rem; background: #16213e; font-size: 0.8rem; }}
    nav a {{ color: #e94560; margin-right: 1rem; text-decoration: none; }}
    nav a:hover {{ text-decoration: underline; }}
    .summary {{ padding: 1rem; display: flex; gap: 1.5rem; font-size: 0.85rem; color: #aaa; }}
    .summary span {{ font-weight: 600; color: #e0e0e0; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ text-align: left; padding: 8px 12px; border-bottom: 2px solid #444; font-size: 0.8rem; color: #aaa; text-transform: uppercase; letter-spacing: 0.05em; }}
    tr:hover {{ background: #16213e; }}
  </style>
</head>
<body>
  <header><h1>Agent Traces</h1></header>
  <nav>
    <a href="/_apx/agent">Chat</a>
    <a href="/_apx/tools">Tools</a>
    <a href="/_apx/traces">Traces</a>
    <a href="/.well-known/agent.json" target="_blank">Agent Card</a>
  </nav>
  <div class="summary">
    <div>Total: <span>{total}</span></div>
    <div>In Progress: <span>{in_progress}</span></div>
    <div>Completed: <span>{completed}</span></div>
    <div>Errors: <span>{errored}</span></div>
  </div>
  <table>
    <thead>
      <tr><th>Trace ID</th><th>Agent</th><th>Status</th><th>Spans</th><th>Duration</th><th>Input</th></tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</body>
</html>"""


def _span_bubble(span: TraceSpan) -> str:
    duration = f"{span.duration_ms / 1000:.1f}s" if span.duration_ms is not None else ""
    dur_html = f'<span class="dur">{duration}</span>' if duration else ""

    if span.type == "request":
        msg = _extract_message(span.input) or "Request received"
        return f"""<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#7986cb;"></div>
      <div class="step-content">
        <div class="step-header"><span class="who" style="color:#7986cb;">Caller</span></div>
        <div class="bubble caller">{escape(msg)}</div>
      </div>
    </div>"""

    if span.type == "llm":
        model = (span.metadata or {}).get("model") or span.name or "LLM"
        model = str(model).replace("databricks-", "")
        input_msg = _extract_message(span.input)
        output_msg = _extract_message(span.output)
        return f"""<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#00bcd4;"></div>
      <div class="step-content">
        <div class="step-header">
          <span class="who" style="color:#00bcd4;">Agent asked {escape(model)}</span>
          {dur_html}
        </div>
        {f'<div class="bubble agent-ask">{escape(input_msg)}</div>' if input_msg else ''}
        {f'<div class="bubble llm-reply">{escape(output_msg)}</div>' if output_msg else ''}
      </div>
    </div>"""

    if span.type == "tool":
        input_msg = _extract_message(span.input)
        output_msg = _extract_message(span.output)
        in_html = (
            f'<div class="bubble tool-in">'
            + "".join(f'<div class="kv">{escape(line)}</div>' for line in input_msg.split("\n"))
            + "</div>"
        ) if input_msg else ""
        out_html = (
            f'<div class="bubble tool-out">'
            + "".join(f'<div class="kv">{escape(line)}</div>' for line in output_msg.split("\n"))
            + "</div>"
        ) if output_msg else ""
        return f"""<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#ffb300;"></div>
      <div class="step-content">
        <div class="step-header">
          <span class="who" style="color:#ffb300;">Called tool <em>{escape(span.name)}</em></span>
          {dur_html}
        </div>
        {in_html}
        {out_html}
      </div>
    </div>"""

    if span.type == "agent_call":
        output_msg = _extract_message(span.output)
        return f"""<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#ab47bc;"></div>
      <div class="step-content">
        <div class="step-header">
          <span class="who" style="color:#ab47bc;">Called agent <em>{escape(span.name)}</em></span>
          {dur_html}
        </div>
        {f'<div class="bubble agent-reply">{escape(output_msg)}</div>' if output_msg else ''}
      </div>
    </div>"""

    if span.type == "response":
        msg = _extract_message(span.output) or "Done"
        return f"""<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#4caf50;"></div>
      <div class="step-content">
        <div class="step-header"><span class="who" style="color:#4caf50;">Agent responded</span></div>
        <div class="bubble response">{escape(msg)}</div>
      </div>
    </div>"""

    if span.type == "error":
        msg = _extract_message(span.output) or "Unknown error"
        return f"""<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#f44336;"></div>
      <div class="step-content">
        <div class="step-header"><span class="who" style="color:#f44336;">Error</span></div>
        <div class="bubble error-msg">{escape(msg)}</div>
      </div>
    </div>"""

    return ""


def _render_trace_detail_ui(trace: Trace) -> str:
    duration = (
        f"{trace.duration_ms / 1000:.1f}s"
        if trace.duration_ms is not None
        else "in progress"
    )
    spans_html = "\n".join(_span_bubble(s) for s in trace.spans) or (
        '<div style="padding:3rem;text-align:center;color:#555;">No steps recorded</div>'
    )
    status_color = (
        "#4caf50" if trace.status == "completed"
        else "#f44336" if trace.status == "error"
        else "#ffb74d"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trace: {escape(trace.agent_name)}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a14; color: #e0e0e0; min-height: 100vh; }}
    .top-bar {{ padding: 12px 20px; background: #12121e; border-bottom: 1px solid #1e1e30; display: flex; align-items: center; gap: 12px; }}
    .top-bar a {{ color: #7986cb; text-decoration: none; font-size: 13px; }}
    .top-bar h1 {{ font-size: 16px; font-weight: 600; flex: 1; }}
    .top-bar .status {{ padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
    .top-bar .meta {{ font-size: 12px; color: #666; }}
    .conversation {{ max-width: 700px; margin: 0 auto; padding: 24px 20px; }}
    .step {{ position: relative; padding-left: 28px; margin-bottom: 4px; }}
    .step-line {{ position: absolute; left: 8px; top: 20px; bottom: -4px; width: 1px; background: #1e1e30; }}
    .step:last-child .step-line {{ display: none; }}
    .step-dot {{ position: absolute; left: 3px; top: 6px; width: 11px; height: 11px; border-radius: 50%; }}
    .step-content {{ padding-bottom: 12px; }}
    .step-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
    .who {{ font-size: 13px; font-weight: 600; }}
    .dur {{ font-size: 11px; color: #555; }}
    .bubble {{ padding: 10px 14px; border-radius: 10px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; max-width: 600px; }}
    .bubble.caller {{ background: #1a1a30; color: #b0b0c8; border: 1px solid #252545; }}
    .bubble.agent-ask {{ background: #0a1a25; color: #80cbc4; border: 1px solid #1a3040; font-size: 13px; }}
    .bubble.llm-reply {{ background: #12222e; color: #e0f0f0; border: 1px solid #1a3545; margin-top: 6px; }}
    .bubble.tool-in {{ background: #1a1800; color: #d4c87a; border: 1px solid #2a2500; font-size: 13px; }}
    .bubble.tool-out {{ background: #1a1a08; color: #e0d8a0; border: 1px solid #2a2810; margin-top: 6px; }}
    .bubble.agent-reply {{ background: #1a0a25; color: #d1a0e8; border: 1px solid #2a1a40; }}
    .bubble.response {{ background: #0a1a0a; color: #a0d8a0; border: 1px solid #1a3020; }}
    .bubble.error-msg {{ background: #1a0a0a; color: #f08080; border: 1px solid #3a1a1a; }}
    .kv {{ padding: 2px 0; }}
  </style>
</head>
<body>
  <div class="top-bar">
    <a href="/_apx/traces">&larr; All traces</a>
    <h1>{escape(trace.agent_name)}</h1>
    <span class="status" style="background:{status_color}20;color:{status_color};">{escape(trace.status or 'unknown')}</span>
    <span class="meta">{duration} &middot; {len(trace.spans)} steps</span>
  </div>
  <div class="conversation">
    {spans_html}
  </div>
</body>
</html>"""
