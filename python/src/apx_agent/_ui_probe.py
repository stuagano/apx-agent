"""Dev UI — /_apx/probe outbound connectivity tester and agent instruction generation."""

from __future__ import annotations

import logging
from typing import Any

from ._models import AgentContext

logger = logging.getLogger(__name__)

def _discover_vs_indexes(ws: "WorkspaceClient") -> list[dict[str, Any]]:
    """Discover available Mosaic AI Vector Search endpoints and indexes.

    Returns a list of dicts with endpoint, index name, source table, ready
    status, and suggested columns — enough to pre-fill VS_INDEX / VS_COLUMNS.
    """
    results: list[dict[str, Any]] = []
    try:
        endpoints = list(ws.vector_search_endpoints.list_endpoints())
    except Exception as e:
        return [{"error": f"Could not list endpoints: {e}"}]

    for ep in endpoints:
        ep_name = ep.name or ""
        ep_state = getattr(getattr(ep, "endpoint_status", None), "state", None)
        ep_state_str = ep_state.value if ep_state is not None else "unknown"

        try:
            indexes_resp = ws.vector_search_indexes.list_indexes(endpoint_name=ep_name)
            raw_indexes = list(getattr(indexes_resp, "vector_indexes", None) or [])
        except Exception:
            raw_indexes = []

        for mini_idx in raw_indexes:
            idx_name = mini_idx.name or ""
            entry: dict[str, Any] = {
                "endpoint": ep_name,
                "endpoint_state": ep_state_str,
                "index": idx_name,
                "source_table": "",
                "ready": False,
                "columns": [],
            }
            try:
                idx = ws.vector_search_indexes.get_index(index_name=idx_name)
                entry["ready"] = bool(getattr(getattr(idx, "status", None), "ready", False))
                spec = getattr(idx, "delta_sync_index_spec", None)
                source_table = getattr(spec, "source_table", None) or ""
                entry["source_table"] = source_table
                emb_cols = getattr(spec, "embedding_source_columns", None) or []
                content_col = emb_cols[0].name if emb_cols else "content"
                columns = [content_col]
                if source_table:
                    try:
                        table_info = ws.tables.get(full_name=source_table)
                        all_cols = [c.name for c in (table_info.columns or []) if c.name]
                        other_cols = [c for c in all_cols if c != content_col and not c.startswith("_")]
                        columns = [content_col] + other_cols
                    except Exception:
                        pass
                entry["columns"] = columns
            except Exception as ex:
                entry["error"] = str(ex)
            results.append(entry)

    return results


def _render_probe_ui(
    result: dict[str, Any] | None = None,
    vs_data: list[dict[str, Any]] | None = None,
) -> str:
    """Return a self-contained HTML page for testing outbound connectivity.

    GET /_apx/probe?url=https://api.example.com renders the form pre-filled.
    The probe runs server-side so results reflect the deployment's network path.
    """
    import json as _json

    result_html = ""
    if result is not None:
        status = result.get("status")
        ok = isinstance(status, int) and status < 400
        color = "#4ade80" if ok else "#f87171"
        rows = "".join(
            f'<tr><td class="k">{k}</td><td class="v">{_json.dumps(v) if not isinstance(v, str) else v}</td></tr>'
            for k, v in result.items()
        )
        result_html = f"""
<section class="result {'ok' if ok else 'err'}">
  <div class="result-head" style="color:{color}">
    {'✓' if ok else '✗'} {result.get('status', result.get('error', 'Error'))}
    {'&nbsp;&nbsp;<span class="latency">' + str(result.get('latency_ms', '')) + ' ms</span>' if 'latency_ms' in result else ''}
  </div>
  <table>{rows}</table>
</section>"""

    vs_html = ""
    if vs_data is not None:
        if not vs_data:
            cards = '<p class="vs-empty">No Vector Search indexes found in this workspace.</p>'
        elif vs_data[0].get("error"):
            cards = f'<p class="vs-error">{vs_data[0]["error"]}</p>'
        else:
            card_parts = []
            for idx in vs_data:
                if idx.get("error"):
                    card_parts.append(
                        f'<div class="vs-card">'
                        f'<div class="vs-card-head"><span class="vs-idx-name">{idx["index"]}</span></div>'
                        f'<p class="vs-error">{idx["error"]}</p>'
                        f'</div>'
                    )
                    continue
                ready = idx.get("ready", False)
                ready_label = "● Ready" if ready else "○ Not ready"
                ready_cls = "ready" if ready else "not-ready"
                cols_repr = _json.dumps(idx.get("columns", []))
                snippet = f'VS_INDEX = "{idx["index"]}"\nVS_COLUMNS = {cols_repr}'
                meta_parts = []
                if idx.get("endpoint"):
                    meta_parts.append(f'endpoint: {idx["endpoint"]}')
                if idx.get("source_table"):
                    meta_parts.append(f'source: {idx["source_table"]}')
                meta = " &nbsp;·&nbsp; ".join(meta_parts)
                card_parts.append(
                    f'<div class="vs-card">'
                    f'  <div class="vs-card-head">'
                    f'    <span class="vs-idx-name">{idx["index"]}</span>'
                    f'    <span class="vs-ready {ready_cls}">{ready_label}</span>'
                    f'  </div>'
                    f'  <div class="vs-meta">{meta}</div>'
                    f'  <pre class="vs-snippet">{snippet}</pre>'
                    f'</div>'
                )
            cards = "".join(card_parts)
        vs_html = f"""
<section class="vs-section">
  <h2 class="vs-title">Vector Search Indexes</h2>
  <p class="vs-desc">Available Mosaic AI Vector Search indexes in this workspace.
  Copy VS_INDEX and VS_COLUMNS into <code>agent_router.py</code> to enable RAG.</p>
  {cards}
</section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Probe — APX Dev</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d0d0d; color: #e8e8e8; min-height: 100vh;
         display: flex; flex-direction: column; }}
  header {{ padding: 12px 20px; background: #111; border-bottom: 1px solid #2a2a2a;
            display: flex; align-items: center; gap: 12px; flex-shrink: 0; }}
  .badge {{ background: #1e3a5f; color: #60b0ff; font-size: 11px; font-weight: 600;
            padding: 2px 8px; border-radius: 4px; letter-spacing: .5px; text-transform: uppercase; }}
  h1 {{ font-size: 16px; font-weight: 600; color: #fff; }}
  nav {{ display: flex; gap: 4px; margin-left: auto; }}
  nav a {{ font-size: 12px; color: #888; text-decoration: none; padding: 3px 10px;
           border-radius: 5px; border: 1px solid transparent; }}
  nav a:hover {{ color: #ccc; border-color: #333; }}
  nav a.active {{ color: #60b0ff; background: #0d1f38; border-color: #1e3a5f; }}
  main {{ padding: 32px 40px; max-width: 760px; }}
  p.desc {{ color: #666; font-size: 13px; margin-bottom: 24px; line-height: 1.6; }}
  .probe-form {{ display: flex; gap: 8px; margin-bottom: 24px; }}
  input[type=text] {{ flex: 1; background: #1a1a1a; border: 1px solid #333; color: #e8e8e8;
                      border-radius: 8px; padding: 9px 14px; font-size: 14px; font-family: monospace;
                      outline: none; }}
  input[type=text]:focus {{ border-color: #3a7bd5; }}
  button {{ background: #2563eb; color: #fff; border: none; border-radius: 8px;
            padding: 9px 18px; font-size: 14px; cursor: pointer; font-weight: 500;
            white-space: nowrap; transition: background .15s; }}
  button:hover {{ background: #1d4ed8; }}
  .result {{ background: #111; border: 1px solid #2a2a2a; border-radius: 8px;
             padding: 16px 20px; }}
  .result.ok {{ border-color: #14532d; }}
  .result.err {{ border-color: #450a0a; }}
  .result-head {{ font-size: 15px; font-weight: 600; margin-bottom: 12px; }}
  .latency {{ font-size: 12px; color: #888; font-weight: 400; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
  td {{ padding: 4px 0; vertical-align: top; }}
  td.k {{ color: #888; width: 140px; font-family: monospace; padding-right: 16px; }}
  td.v {{ color: #ccc; font-family: monospace; word-break: break-all; }}
  .vs-section {{ margin-top: 40px; }}
  .vs-title {{ font-size: 14px; font-weight: 600; color: #888; text-transform: uppercase;
               letter-spacing: .6px; margin-bottom: 8px; }}
  .vs-desc {{ color: #555; font-size: 12px; margin-bottom: 16px; line-height: 1.6; }}
  .vs-card {{ background: #111; border: 1px solid #2a2a2a; border-radius: 8px;
              padding: 14px 16px; margin-bottom: 12px; }}
  .vs-card-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }}
  .vs-idx-name {{ font-family: monospace; font-size: 13px; color: #e8e8e8; font-weight: 500; }}
  .vs-ready {{ font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 10px; }}
  .vs-ready.ready {{ color: #4ade80; background: #052e16; }}
  .vs-ready.not-ready {{ color: #f87171; background: #2a0a0a; }}
  .vs-meta {{ font-size: 11px; color: #555; margin-bottom: 10px; font-family: monospace; }}
  .vs-snippet {{ background: #0d0d0d; border: 1px solid #222; border-radius: 6px;
                 padding: 10px 12px; font-size: 12px; font-family: monospace; color: #a5f3fc;
                 white-space: pre; overflow-x: auto; }}
  .vs-error {{ color: #f87171; font-size: 12px; font-family: monospace; }}
  .vs-empty {{ color: #444; font-size: 12px; font-style: italic; }}
</style>
</head>
<body>
<header>
  <span class="badge">APX dev</span>
  <h1>Probe</h1>
  <nav>
    <a href="/_apx/agent">Chat</a>
    <a href="/_apx/tools">Tools</a>
    <a href="/_apx/edit">Edit</a>
    <a href="/_apx/probe" class="active">Probe</a>
    <a href="/_apx/setup">Setup</a>
    <a href="/_apx/eval">Eval</a>
    <a href="/_apx/wizard">Wizard</a>
  </nav>
  <button id="btn-deploy">Deploy ▶</button>
</header>
<main>
  <p class="desc">
    Test outbound connectivity from this deployment. The request runs server-side,
    so the result reflects the network path available to your deployed app — not your browser.
  </p>
  <form class="probe-form" method="get" action="/_apx/probe">
    <input type="text" name="url" placeholder="https://api.example.com/health"
           value="{{url_prefill}}" autofocus>
    <button type="submit">Probe</button>
  </form>
  {result_html}
  {vs_html}
</main>
{_deploy_overlay_html()}
</body>
</html>"""


async def _run_probe(url: str) -> dict[str, Any]:
    """Make an outbound GET request and return connectivity diagnostics."""
    import time
    import ssl
    import httpx

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            resp = await client.get(url)
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "url": str(resp.url),
            "status": resp.status_code,
            "latency_ms": latency_ms,
            "content_type": resp.headers.get("content-type", ""),
            "server": resp.headers.get("server", ""),
            "redirects": len(resp.history),
        }
    except httpx.ConnectError as e:
        return {"url": url, "error": "ConnectError", "detail": str(e)}
    except httpx.TimeoutException:
        return {"url": url, "error": "Timeout", "detail": "No response within 10 s"}
    except ssl.SSLError as e:
        return {"url": url, "error": "SSLError", "detail": str(e)}
    except Exception as e:
        return {"url": url, "error": type(e).__name__, "detail": str(e)}


# ---------------------------------------------------------------------------


async def _generate_agent_instructions(
    ws: Any,
    ctx: "AgentContext | None",
    catalog: str,
    schema: str,
    warehouse_id: str,
) -> str:
    """Call the LLM to generate domain-specific agent instructions from a UC schema."""
    import asyncio as _asyncio
    from httpx import AsyncClient

    # Fetch table/column list
    tables: dict[str, list[str]] = {}
    if catalog and schema and warehouse_id:
        def _fetch() -> dict[str, list[str]]:
            resp = ws.statement_execution.execute_statement(
                warehouse_id=warehouse_id,
                statement=(
                    f"SELECT table_name, column_name, data_type "
                    f"FROM information_schema.columns "
                    f"WHERE table_schema = '{schema}' "
                    f"ORDER BY table_name, ordinal_position"
                ),
                catalog=catalog,
                schema=schema,
            )
            if not resp.result or not resp.result.data_array:
                return {}
            col_names = [c.name for c in resp.manifest.schema.columns]
            result: dict[str, list[str]] = {}
            for row in resp.result.data_array:
                r = dict(zip(col_names, row))
                result.setdefault(r["table_name"], []).append(
                    f"{r['column_name']}({r['data_type']})"
                )
            return result
        try:
            tables = await _asyncio.to_thread(_fetch)
        except Exception:
            pass

    schema_summary = "\n".join(
        f"  {t}: {', '.join(cols[:12])}" for t, cols in tables.items()
    ) if tables else f"(schema: {catalog}.{schema})"

    system_msg = (
        "You are configuring an AI agent. Write a system prompt (6-10 sentences) "
        "that serves as the agent's operating charter — not just a description, but "
        "actionable decision rules the agent follows on every request.\n\n"
        "The prompt MUST include:\n"
        "1. A one-sentence persona/role statement naming the domain.\n"
        "2. The first tool to call on every session (e.g. to establish context or identity).\n"
        "3. The natural call chain for the 2-3 most common request types given the tables "
        "   (e.g. 'To explain a bill: call get_customer_profile → get_billing_summary → get_rate_schedule').\n"
        "4. A recovery rule: when a tool returns empty or an error dict, try an alternative "
        "   approach before telling the user you can't help — name the alternative.\n"
        "5. A grounding rule: always use tool results, never guess or hallucinate data values.\n\n"
        "Output ONLY the system prompt text — no explanation, no quotes, no markdown."
    )
    user_msg = f"Schema: {catalog}.{schema}\n\nTables:\n{schema_summary}"

    if ctx is None:
        return f"You are a helpful data agent for {catalog}.{schema}. Use your tools to answer questions about the data — never guess."

    auth_headers = ws.config.authenticate()
    endpoint_url = (
        f"{ws.config.host.rstrip('/')}"
        f"/serving-endpoints/{ctx.config.model}/invocations"
    )

    try:
        async with AsyncClient() as client:
            r = await client.post(
                endpoint_url,
                headers={**auth_headers, "Content-Type": "application/json"},
                json={
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 500,
                    "temperature": 0.3,
                },
                timeout=30.0,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"You are a helpful data agent for {catalog}.{schema}. Use your tools to answer questions — never guess. ({e})"


