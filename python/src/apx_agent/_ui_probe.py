"""Dev UI — /_apx/probe outbound connectivity tester and agent instruction generation."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ._models import AgentContext
from ._ui_nav import _apx_nav_links, _deploy_overlay_html
from ._ui_setup import _find_env_path, _read_env_file

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health checks — surfaced at /_apx/probe/checks
# ---------------------------------------------------------------------------

# Per-check timeout. The endpoint runs all checks in parallel so wall time
# is roughly max(check_durations) + overhead.
_CHECK_TIMEOUT_S = 4.0


async def _check_workspace_auth() -> dict[str, Any]:
    """Confirm a Databricks WorkspaceClient can authenticate."""
    try:
        from databricks.sdk import WorkspaceClient

        def _init() -> str:
            ws = WorkspaceClient()
            host = ws.config.host or "?"
            return host

        host = await asyncio.wait_for(asyncio.to_thread(_init), timeout=_CHECK_TIMEOUT_S)
        return {
            "name": "workspace_auth",
            "status": "ok",
            "message": f"Authenticated against {host}",
            "hint": "",
        }
    except asyncio.TimeoutError:
        return {
            "name": "workspace_auth",
            "status": "fail",
            "message": "WorkspaceClient init timed out",
            "hint": "Check DATABRICKS_HOST and credentials.",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "workspace_auth",
            "status": "fail",
            "message": str(exc)[:200],
            "hint": "Run `databricks auth login` or set DATABRICKS_HOST + DATABRICKS_TOKEN.",
        }


async def _check_model(ctx: AgentContext | None) -> dict[str, Any]:
    """Issue a one-shot LLM call to ctx.config.model."""
    if ctx is None or not getattr(ctx.config, "model", ""):
        return {
            "name": "model",
            "status": "skip",
            "message": "No model configured",
            "hint": "Set `model` in pyproject.toml [tool.apx_agent].",
        }

    model = ctx.config.model
    try:
        from databricks_openai import AsyncDatabricksOpenAI

        async def _call() -> str:
            client = AsyncDatabricksOpenAI()
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
            )
            choices = getattr(resp, "choices", None) or []
            if not choices:
                return ""
            msg = getattr(choices[0], "message", None)
            return (getattr(msg, "content", None) or "") if msg else ""

        out = await asyncio.wait_for(_call(), timeout=_CHECK_TIMEOUT_S)
        if out:
            return {
                "name": "model",
                "status": "ok",
                "message": f"{model} responded ({len(out)} chars)",
                "hint": "",
            }
        return {
            "name": "model",
            "status": "warn",
            "message": f"{model} returned empty output",
            "hint": "Model is reachable but produced no text — check rate limits or model availability.",
        }
    except asyncio.TimeoutError:
        return {
            "name": "model",
            "status": "fail",
            "message": f"{model} timed out after {_CHECK_TIMEOUT_S}s",
            "hint": "Model may be cold-starting or unreachable from this network.",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "model",
            "status": "fail",
            "message": f"{model}: {str(exc)[:160]}",
            "hint": "Verify DATABRICKS_HOST + the model serving endpoint is deployed.",
        }


async def _check_env_vars(ctx: AgentContext | None) -> dict[str, Any]:
    """Compare keys declared in .env against the running process."""
    env_path = _find_env_path()
    declared: dict[str, str] = {}
    if env_path and env_path.exists():
        try:
            declared = _read_env_file(env_path)
        except Exception:
            return {
                "name": "env_vars",
                "status": "warn",
                "message": f"Failed to read {env_path}",
                "hint": "Check the file syntax.",
            }

    if not declared:
        return {
            "name": "env_vars",
            "status": "skip",
            "message": "No .env file found — skipping",
            "hint": "Add a .env via /_apx/setup if your tools need configuration.",
        }

    missing = [k for k, v in declared.items() if v and not os.environ.get(k)]
    if not missing:
        return {
            "name": "env_vars",
            "status": "ok",
            "message": f"All {len(declared)} declared vars present in process",
            "hint": "",
        }
    return {
        "name": "env_vars",
        "status": "warn",
        "message": f"Missing in process: {', '.join(missing[:5])}" + ("…" if len(missing) > 5 else ""),
        "hint": "Restart the dev server after editing .env, or export the vars manually.",
    }


async def _check_sub_agent(name: str, url: str) -> dict[str, Any]:
    """Hit /.well-known/agent.json to confirm reachability."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT_S) as client:
            resp = await client.get(f"{url.rstrip('/')}/.well-known/agent.json")
        if resp.status_code == 200:
            return {
                "name": f"sub_agent: {name}",
                "status": "ok",
                "message": f"{url} responded 200",
                "hint": "",
            }
        return {
            "name": f"sub_agent: {name}",
            "status": "warn",
            "message": f"{url} returned {resp.status_code}",
            "hint": "Sub-agent is reachable but did not return its agent card. Check deployment status.",
        }
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return {
            "name": f"sub_agent: {name}",
            "status": "fail",
            "message": f"{url} timed out after {_CHECK_TIMEOUT_S}s",
            "hint": "Verify the sub-agent is running and reachable from this network.",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": f"sub_agent: {name}",
            "status": "fail",
            "message": f"{url}: {str(exc)[:160]}",
            "hint": "Confirm the URL in pyproject.toml [tool.apx_agent.sub_agents].",
        }


async def _gather_sub_agent_checks(ctx: AgentContext | None) -> list[dict[str, Any]]:
    sub_agents = (
        getattr(ctx.config, "sub_agents", None) or []
        if ctx is not None
        else []
    )
    if not sub_agents:
        return [{
            "name": "sub_agents",
            "status": "skip",
            "message": "No sub-agents configured",
            "hint": "Add URLs to pyproject.toml [tool.apx_agent].sub_agents to enable.",
        }]
    # Resolve $VAR / ${VAR} env refs the same way _wiring.py does.
    resolved: list[tuple[str, str]] = []
    for raw in sub_agents:
        url = raw
        if isinstance(url, str) and url.startswith("$"):
            url = os.environ.get(url.lstrip("$").strip("{}"), "")
        if url:
            resolved.append((raw, url))
    if not resolved:
        return [{
            "name": "sub_agents",
            "status": "warn",
            "message": "All sub-agent URLs resolved to empty (env refs unset)",
            "hint": "Set the env vars referenced by sub_agents entries.",
        }]
    return list(await asyncio.gather(*[_check_sub_agent(raw, url) for raw, url in resolved]))


async def _check_mlflow_config() -> dict[str, Any]:
    """Verify MLFLOW_TRACKING_URI + MLFLOW_EXPERIMENT_ID look sane."""
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "")
    experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID", "")

    if not tracking_uri:
        return {
            "name": "mlflow_config",
            "status": "warn",
            "message": "MLFLOW_TRACKING_URI not set — traces go to local SQLite",
            "hint": "Add MLFLOW_TRACKING_URI=databricks to app.yaml env or .env file.",
        }
    if tracking_uri != "databricks":
        return {
            "name": "mlflow_config",
            "status": "warn",
            "message": f"MLFLOW_TRACKING_URI={tracking_uri!r} (expected 'databricks')",
            "hint": "Set MLFLOW_TRACKING_URI=databricks to use the workspace MLflow backend.",
        }
    if not experiment_id:
        return {
            "name": "mlflow_config",
            "status": "warn",
            "message": "MLFLOW_EXPERIMENT_ID not set — traces land in the default experiment",
            "hint": "Set MLFLOW_EXPERIMENT_ID to pin the experiment. Find the ID in the MLflow UI URL.",
        }

    # Verify the experiment actually exists and is reachable.
    try:
        from mlflow.tracking import MlflowClient as _MlflowClient

        def _verify() -> str:
            exp = _MlflowClient().get_experiment(experiment_id)
            return getattr(exp, "name", experiment_id)

        exp_name = await asyncio.wait_for(
            asyncio.to_thread(_verify), timeout=_CHECK_TIMEOUT_S
        )
        return {
            "name": "mlflow_config",
            "status": "ok",
            "message": f"Experiment {experiment_id!r} ({exp_name}) reachable",
            "hint": "",
        }
    except ImportError:
        return {
            "name": "mlflow_config",
            "status": "skip",
            "message": "mlflow not installed",
            "hint": "Install apx-agent[eval] to enable MLflow tracing.",
        }
    except asyncio.TimeoutError:
        return {
            "name": "mlflow_config",
            "status": "warn",
            "message": f"MLflow experiment lookup timed out after {_CHECK_TIMEOUT_S}s",
            "hint": "Workspace may be slow or MLFLOW_EXPERIMENT_ID may be wrong.",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "mlflow_config",
            "status": "fail",
            "message": f"Experiment {experiment_id!r} not found: {str(exc)[:160]}",
            "hint": "Verify MLFLOW_EXPERIMENT_ID matches an experiment in this workspace.",
        }


async def _check_mlflow_export() -> dict[str, Any]:
    """Check for recent MLflow trace-export failures (e.g. blob-storage blocked)."""
    try:
        from ._mlflow_tracing import _mlflow_export_errors
    except ImportError:
        return {
            "name": "mlflow_export",
            "status": "skip",
            "message": "mlflow not installed",
            "hint": "",
        }

    errors = list(_mlflow_export_errors)
    if not errors:
        return {
            "name": "mlflow_export",
            "status": "ok",
            "message": "No MLflow export failures recorded",
            "hint": "",
        }

    import re

    # Categorize the most recent error by signature so the hint is specific.
    last = errors[-1]
    host_match = re.search(r"([\w.-]+\.storage\.cloud\.databricks\.com)", last)
    perm_match = re.search(r"permission denied|PERMISSION_DENIED|\b403\b", last, re.I)
    exp_match = re.search(r"[Ee]xperiment .* does not exist|RESOURCE_DOES_NOT_EXIST", last)

    if host_match:
        category = "egress_blocked"
        hint = (
            f"Egress to {host_match.group(1)} is blocked. This is expected on "
            "FEVM/private-link workspaces. Trace metadata still saves; the "
            "list view uses include_spans=False so it keeps working."
        )
    elif exp_match:
        category = "experiment_missing"
        hint = (
            "The MLflow experiment does not exist. Verify MLFLOW_EXPERIMENT_ID "
            "matches an experiment in this workspace (see mlflow_config)."
        )
    elif perm_match:
        category = "write_denied"
        hint = (
            "Trace writes are being denied. The app's service principal needs "
            "EDIT on the MLflow experiment. Grant it in the experiment's "
            "permissions, or point MLFLOW_EXPERIMENT_ID at an owned experiment."
        )
    else:
        category = "unknown"
        hint = "Check app logs for the full mlflow.tracing.export error."

    return {
        "name": "mlflow_export",
        "status": "warn",
        "message": f"{len(errors)} export failure(s) [{category}]. Last: {last[:180]}",
        "hint": hint,
    }


# Maps ResourceSpec.kind -> (human label, positional getter on WorkspaceClient).
# Positional args are used deliberately: the SDK has renamed these keyword
# params across versions (e.g. functions.get full_name -> name), but positional
# calls stay stable.
def _verify_resource(ws: Any, kind: str, ident: str) -> None:
    """Call the SDK getter for ``ident``. Raises if it doesn't resolve."""
    if kind == "uc_function":
        ws.functions.get(ident)
    elif kind == "uc_table":
        ws.tables.get(ident)
    elif kind == "genie_space":
        ws.genie.get_space(ident)
    elif kind == "sql_warehouse":
        ws.warehouses.get(ident)
    elif kind == "serving_endpoint":
        ws.serving_endpoints.get(ident)
    elif kind == "vector_search_index":
        ws.vector_search_indexes.get_index(ident)
    else:  # pragma: no cover — unknown kind, treat as unverifiable
        raise ValueError(f"no verifier for resource kind {kind!r}")


async def _check_resources(ctx: AgentContext | None) -> dict[str, Any]:
    """Verify every declared governed resource resolves at runtime.

    Covers all ResourceSpec kinds reachable from the agent's tool tree — UC
    functions/tables, Genie spaces, SQL warehouses, serving endpoints, and
    vector search indexes. Catches the "tool 403s or returns nothing" class of
    failure across every governed primitive, not just UC functions.
    """
    if ctx is None:
        return {
            "name": "resources",
            "status": "skip",
            "message": "No agent context",
            "hint": "",
        }

    try:
        from ._resources import _iter_tool_fns, get_resources
    except ImportError:
        return {
            "name": "resources",
            "status": "skip",
            "message": "Resource helpers not available",
            "hint": "",
        }

    # Collect unique (kind, identifier) pairs across the tool tree.
    seen: set[tuple[str, str]] = set()
    specs: list[tuple[str, str]] = []
    _verifiable = {
        "uc_function", "uc_table", "genie_space",
        "sql_warehouse", "serving_endpoint", "vector_search_index",
    }
    try:
        for fn in _iter_tool_fns(ctx.agent):
            for spec in get_resources(fn):
                key = (spec.kind, spec.identifier)
                if spec.kind in _verifiable and key not in seen:
                    seen.add(key)
                    specs.append(key)
    except Exception:
        pass

    if not specs:
        return {
            "name": "resources",
            "status": "skip",
            "message": "No governed resources declared by tools",
            "hint": "Add uc_function_tool / genie_tool / vector_search_tool to enable.",
        }

    def _verify_all() -> list[tuple[str, str, str]]:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()
        failures: list[tuple[str, str, str]] = []
        for kind, ident in specs:
            try:
                _verify_resource(ws, kind, ident)
            except Exception as exc:  # noqa: BLE001
                failures.append((kind, ident, str(exc)[:100]))
        return failures

    try:
        failures = await asyncio.wait_for(
            asyncio.to_thread(_verify_all), timeout=_CHECK_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return {
            "name": "resources",
            "status": "warn",
            "message": f"Resource lookup timed out after {_CHECK_TIMEOUT_S}s ({len(specs)} resources)",
            "hint": "Workspace may be slow. Check DATABRICKS_HOST and credentials.",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "resources",
            "status": "fail",
            "message": f"Could not check resources: {str(exc)[:160]}",
            "hint": "Verify WorkspaceClient can authenticate (see workspace_auth check).",
        }

    if not failures:
        return {
            "name": "resources",
            "status": "ok",
            "message": f"All {len(specs)} governed resource(s) reachable",
            "hint": "",
        }

    summary = "; ".join(f"{kind} {ident}: {err}" for kind, ident, err in failures[:3])
    if len(failures) > 3:
        summary += f" (+ {len(failures) - 3} more)"
    return {
        "name": "resources",
        "status": "fail",
        "message": f"{len(failures)}/{len(specs)} resource(s) unreachable: {summary}",
        "hint": "Grant the app's service principal access, or check the identifier.",
    }


def _running_in_apps(headers: dict[str, str] | None) -> bool:
    """True when this process looks like a Databricks Apps deployment.

    Detected via the Apps-injected port env var or any ``X-Forwarded-*``
    header on the inbound request (the Apps proxy sets these; bare uvicorn
    does not).
    """
    if os.environ.get("DATABRICKS_APP_PORT"):
        return True
    if headers:
        return any(k.lower().startswith("x-forwarded-") for k in headers)
    return False


async def _check_obo(headers: dict[str, str] | None) -> dict[str, Any]:
    """Report whether the calling user's OBO token is flowing on this request.

    In Databricks Apps the proxy injects ``X-Forwarded-Access-Token`` on every
    request (including this probe). When identity passthrough breaks, every
    tool call 403s opaquely — this surfaces the root cause directly.

    Outside Apps (local uvicorn) there is no proxy and no header, so the check
    skips rather than failing every local dev session.
    """
    if not _running_in_apps(headers):
        return {
            "name": "obo_identity",
            "status": "skip",
            "message": "Not running in Databricks Apps — no OBO proxy",
            "hint": "Identity passthrough only applies to deployed Apps requests.",
        }

    from ._obo import _header_lookup

    hdrs = headers or {}
    token = _header_lookup(hdrs, "X-Forwarded-Access-Token")  # type: ignore[arg-type]
    user_email = _header_lookup(hdrs, "X-Forwarded-Email")  # type: ignore[arg-type]

    if not token:
        return {
            "name": "obo_identity",
            "status": "fail",
            "message": "No X-Forwarded-Access-Token on this request",
            "hint": (
                "User Authorization (OBO) is not enabled for this App, or the "
                "scopes are missing. Enable 'User authorization' in the App's "
                "settings so tools run as the calling user."
            ),
        }
    who = f" (user: {user_email})" if user_email else ""
    return {
        "name": "obo_identity",
        "status": "ok",
        "message": f"OBO token present on request{who}",
        "hint": "",
    }


async def _check_session_store(session_store: Any | None) -> dict[str, Any]:
    """Ping the configured session backend with a non-destructive read."""
    if session_store is None:
        return {
            "name": "session_store",
            "status": "skip",
            "message": "No session store configured",
            "hint": "Pass session_store= to enable multi-turn history.",
        }

    store_kind = type(session_store).__name__
    # InMemory has no backend to reach — report it but don't ping.
    if store_kind == "InMemorySessionStore":
        return {
            "name": "session_store",
            "status": "ok",
            "message": f"{store_kind} (in-process, no backend)",
            "hint": "InMemory history is lost on restart — use Lakebase/Delta for durability.",
        }

    def _ping() -> None:
        # get() of a non-existent id returns None but still exercises the
        # backend connection (and surfaces auth/network errors).
        session_store.get("__apx_probe_healthcheck__")

    try:
        await asyncio.wait_for(asyncio.to_thread(_ping), timeout=_CHECK_TIMEOUT_S)
        return {
            "name": "session_store",
            "status": "ok",
            "message": f"{store_kind} backend reachable",
            "hint": "",
        }
    except asyncio.TimeoutError:
        return {
            "name": "session_store",
            "status": "fail",
            "message": f"{store_kind} ping timed out after {_CHECK_TIMEOUT_S}s",
            "hint": "Backend is slow or unreachable — history will silently fail to load.",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "session_store",
            "status": "fail",
            "message": f"{store_kind}: {str(exc)[:160]}",
            "hint": "Backend unreachable — verify connection settings and credentials.",
        }


async def _check_agent_source() -> dict[str, Any]:
    """Confirm agent.py parses and every tool referenced in an ``Agent(tools=[...])``
    is a defined/imported name — catches a half-applied dev-UI edit or a tool that
    was removed but still listed (valid syntax, runtime NameError)."""
    try:
        from ._ui_edit import _find_agent_router_path, _parse_agent_nodes
    except ImportError:
        return {"name": "agent_source", "status": "skip", "message": "editor helpers unavailable", "hint": ""}

    path = _find_agent_router_path()
    if not path or not path.exists():
        return {"name": "agent_source", "status": "skip", "message": "agent.py not found", "hint": ""}

    import ast as _ast

    src = path.read_text()
    try:
        tree = _ast.parse(src)
    except SyntaxError as exc:
        return {
            "name": "agent_source", "status": "fail",
            "message": f"agent.py syntax error (line {exc.lineno}): {exc.msg}",
            "hint": "A dev-UI edit may have left it broken — open /_apx/edit to fix.",
        }

    known: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            known.add(node.name)
    for stmt in tree.body:
        if isinstance(stmt, _ast.Assign):
            known.update(t.id for t in stmt.targets if isinstance(t, _ast.Name))
        elif isinstance(stmt, (_ast.Import, _ast.ImportFrom)):
            known.update(a.asname or a.name for a in stmt.names)

    dangling = [
        f"{n['name']}: {tool}"
        for n in _parse_agent_nodes(src)
        for tool in n.get("tools", [])
        if tool not in known
    ]
    if dangling:
        return {
            "name": "agent_source", "status": "fail",
            "message": "tools reference undefined names: " + ", ".join(dangling[:5]),
            "hint": "A tool was removed/renamed but is still in tools=[...]. Re-add it or drop the reference.",
        }
    return {
        "name": "agent_source", "status": "ok",
        "message": "agent.py parses; all tool references resolve", "hint": "",
    }


async def _run_probe_checks(
    ctx: AgentContext | None,
    *,
    headers: dict[str, str] | None = None,
    session_store: Any | None = None,
) -> dict[str, Any]:
    """Run every health check in parallel and assemble a single response."""
    (
        workspace, model, env_vars, sub_agents,
        mlflow_cfg, mlflow_exp, resources, obo, session, agent_src,
    ) = await asyncio.gather(
        _check_workspace_auth(),
        _check_model(ctx),
        _check_env_vars(ctx),
        _gather_sub_agent_checks(ctx),
        _check_mlflow_config(),
        _check_mlflow_export(),
        _check_resources(ctx),
        _check_obo(headers),
        _check_session_store(session_store),
        _check_agent_source(),
    )
    checks: list[dict[str, Any]] = [  # type: ignore[list-item]
        workspace, model, env_vars, *sub_agents,
        mlflow_cfg, mlflow_exp, resources, obo, session, agent_src,
    ]
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    overall = "fail" if counts["fail"] else ("warn" if counts["warn"] else "ok")
    return {"overall": overall, "counts": counts, "checks": checks}

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
  /* Health checks */
  .checks-section {{ margin-bottom: 32px; }}
  .check-row {{ display: flex; align-items: flex-start; gap: 12px; padding: 12px 14px;
                 background: #111; border: 1px solid #1f1f1f; border-radius: 8px;
                 margin-bottom: 8px; }}
  .check-dot {{ width: 10px; height: 10px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; }}
  .check-body {{ flex: 1; min-width: 0; }}
  .check-name {{ font-size: 13px; color: #e8e8e8; font-weight: 500; }}
  .check-msg {{ font-size: 12px; color: #888; margin-top: 2px; word-break: break-word; }}
  .check-hint {{ font-size: 11px; color: #555; margin-top: 4px; font-style: italic; }}
  .check-status {{ font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 10px;
                    text-transform: uppercase; letter-spacing: .5px; flex-shrink: 0;
                    align-self: flex-start; margin-top: 2px; }}
  .check-status-ok {{ color: #4ade80; background: #052e16; }}
  .check-status-warn {{ color: #facc15; background: #2a2400; }}
  .check-status-fail {{ color: #f87171; background: #2a0a0a; }}
  .check-status-skip {{ color: #666; background: #1a1a1a; }}
</style>
</head>
<body>
<header>
  <span class="badge">APX dev</span>
  <h1>Probe</h1>
  <nav>{_apx_nav_links("probe")}</nav>
  <button id="btn-deploy">Deploy ▶</button>
</header>
<main>
  <p class="desc">
    Test outbound connectivity from this deployment. The request runs server-side,
    so the result reflects the network path available to your deployed app — not your browser.
  </p>
  <section class="checks-section">
    <h2 class="vs-title">Health checks</h2>
    <p class="vs-desc">Live status of the agent's dependencies. Refresh the page to re-run.</p>
    <div id="checks-list"><p class="vs-empty">Running checks…</p></div>
  </section>
  <form class="probe-form" method="get" action="/_apx/probe" style="margin-top:32px">
    <input type="text" name="url" placeholder="https://api.example.com/health" autofocus>
    <button type="submit">Probe URL</button>
  </form>
  {result_html}
  {vs_html}
</main>
<script>
(async () => {{
  const list = document.getElementById('checks-list');
  const dot = (s) => ({{ok:'#4ade80', warn:'#facc15', fail:'#f87171', skip:'#444'}})[s] || '#888';
  try {{
    const r = await fetch('/_apx/probe/checks');
    const data = await r.json();
    if (!data.checks || !data.checks.length) {{
      list.innerHTML = '<p class="vs-empty">No checks ran.</p>';
      return;
    }}
    list.innerHTML = data.checks.map(c => `
      <div class="check-row">
        <span class="check-dot" style="background:${{dot(c.status)}}"></span>
        <div class="check-body">
          <div class="check-name">${{c.name}}</div>
          <div class="check-msg">${{c.message || ''}}</div>
          ${{c.hint ? `<div class="check-hint">${{c.hint}}</div>` : ''}}
        </div>
        <span class="check-status check-status-${{c.status}}">${{c.status}}</span>
      </div>`).join('');
  }} catch (e) {{
    list.innerHTML = `<p class="vs-error">Failed to load checks: ${{e.message}}</p>`;
  }}
}})();
</script>
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


# Schema introspection + instruction generation now live in _schema.py (UI-free,
# shared with DataAgent). Kept here as thin wrappers for back-compat.
from ._schema import build_instructions_from_schema as _build_instructions_from_schema  # noqa: E402
from ._schema import introspect_schema as _introspect_schema  # noqa: E402


async def _generate_agent_instructions(
    ws: Any,
    ctx: "AgentContext | None",
    catalog: str,
    schema: str,
    warehouse_id: str,
) -> str:
    """Fetch schema metadata then build instructions via Python template (no LLM)."""
    import asyncio as _asyncio

    tables = await _asyncio.to_thread(
        _introspect_schema, ws, catalog, schema, warehouse_id
    )
    return _build_instructions_from_schema(catalog, schema, tables)


