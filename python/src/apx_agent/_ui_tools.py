"""Tool inspector — the real logic of every registered tool, with an editor.

Served at ``/_apx/tools`` and reachable as the Tools tab of the unified dev
console (``/_apx/agent``) and as the Edit page's right-panel sub-tab.

Answers the questions the chat UI's tool cards do not:

* **What does it actually do?** — the function source, read from the file that
  defines it (``inspect.getsourcefile``), with line numbers. When a deployment
  ships a synthetic ``<name>_demo`` twin, it is shown too and labelled with the
  branch this process actually takes.
* **Is that the code that is running?** — the mode flag, the resolved runtime
  constants and the model-facing tool surface, read from the *imported modules*
  rather than from a deploy file, so a running process that disagrees with the
  repo is visible instead of invisible.
* **Can I change it here?** — an editor that rewrites the function in place and
  reports exactly what landed: the running container's copy, the app's
  workspace source copy (durable across restarts), or neither.

Everything is derived from the live runtime: the tool functions are whatever
the agent registered, and the source files are whatever those functions were
loaded from. No module name, table, or tool list is hardcoded, so the page
works for any apx-agent app.
"""

from __future__ import annotations

import ast
import inspect
import io
import linecache
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Module scanned for ``<name>_demo`` twins when a deployment keeps its
# synthetic responses separate from the tools themselves.
_DEMO_MODULE = "demo_data"


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def _source(fn: Any) -> str:
    if fn is None:
        return ""
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return ""


def _source_line(fn: Any) -> int:
    try:
        return inspect.getsourcelines(fn)[1]
    except (OSError, TypeError):
        return 0


def _source_file(fn: Any) -> Path | None:
    """The file ``fn`` was loaded from — the save target and source of spans."""
    try:
        origin = inspect.getsourcefile(fn)
    except TypeError:
        origin = None
    return Path(origin) if origin else None


def _file_text(path: "Path | None") -> str:
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("tool source unreadable (%s): %s", path, exc)
        return ""


@dataclass(frozen=True)
class _ToolInventory:
    """Registered tool functions, and how they were found.

    ``how`` is ``"agent"`` when the list came from the live agent and
    ``"module"`` when it had to be recovered by name from loaded modules
    because no agent is attached to the app state.
    """

    functions: list[Any]
    how: str


def _tool_functions(request: Request) -> _ToolInventory:
    """The agent's registered tools, in wiring order."""
    ctx = getattr(request.app.state, "agent_context", None)
    fns = [
        fn
        for fn in (getattr(getattr(ctx, "agent", None), "_tool_fns", None) or [])
        if callable(fn)
    ]
    if fns:
        return _ToolInventory(fns, "agent")

    # Recover: match the model-facing tool names against top-level functions in
    # loaded (non-framework) modules. Best effort — the source is still real,
    # but descriptions/schemas below come from the module rather than a live
    # tool object.
    wanted = {
        getattr(tool, "name", "")
        for tool in (getattr(ctx, "tools", None) or [])
        if getattr(tool, "name", "")
    }
    if not wanted:
        return _ToolInventory([], "module")
    recovered: list[Any] = []
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("apx_agent"):
            continue
        for name in list(wanted):
            fn = getattr(module, name, None)
            if inspect.isfunction(fn) and getattr(fn, "__name__", "") == name:
                recovered.append(fn)
                wanted.discard(name)
    recovered.sort(key=_source_line)
    return _ToolInventory(recovered, "module")


def _demo_module() -> Any | None:
    """``demo_data``, imported on demand.

    A live deployment never calls it (the tools short-circuit on their own mode
    flag before the import), so it may not be in ``sys.modules`` yet. Pulling it
    in here is what lets the page show the branch that is *not* running.
    """
    module = sys.modules.get(_DEMO_MODULE)
    if module is not None:
        return module
    try:
        import importlib

        return importlib.import_module(_DEMO_MODULE)
    except Exception as exc:  # noqa: BLE001 — inspection is best-effort
        logger.debug("%s not importable: %s", _DEMO_MODULE, exc)
        return None


def _demo_twin(fn: Any, demo_module: Any | None) -> Any | None:
    """``<name>_demo`` — in the tool's own module, else in ``demo_data``."""
    name = getattr(fn, "__name__", "")
    if not name:
        return None
    own = sys.modules.get(getattr(fn, "__module__", ""))
    if own is not None:
        twin = getattr(own, f"{name}_demo", None)
        if callable(twin) and twin is not fn:
            return twin
    if demo_module is not None:
        twin = getattr(demo_module, f"{name}_demo", None)
        if callable(twin):
            return twin
    return None


def _runtime_config(functions: list[Any]) -> dict[str, Any]:
    """The scalar module-level constants bound in the tool modules.

    Derived from the modules rather than a hardcoded key list, so a renamed or
    added setting shows up (or disappears) without this file drifting.
    """
    out: dict[str, Any] = {}
    for mod_name in sorted({getattr(fn, "__module__", "") for fn in functions}):
        module = sys.modules.get(mod_name)
        if module is None:
            continue
        for name, value in vars(module).items():
            if name.startswith("_") or not name.isupper():
                continue
            if isinstance(value, (str, int, float, bool)) and name not in out:
                out[name] = value
        demo_flag = getattr(module, "_demo_mode", None)
        if callable(demo_flag) and "DEMO_MODE_EFFECTIVE" not in out:
            try:
                out["DEMO_MODE_EFFECTIVE"] = bool(demo_flag())
            except Exception:  # noqa: BLE001 — defensive
                pass
    return dict(sorted(out.items()))


def _context_status(request: Request) -> dict[str, Any]:
    state = request.app.state
    ctx = getattr(state, "agent_context", None)
    return {
        "agent_context": ctx is not None,
        "llm_tool_count": len(list(getattr(ctx, "tools", None) or [])) if ctx else 0,
        "dev_ui_mount_error": getattr(state, "dev_ui_mount_error", None),
        "mcp_mount_error": getattr(state, "mcp_mount_error", None),
    }


def _connection_status(functions: list[Any], config: dict[str, Any]) -> dict[str, Any]:
    demo = bool(config.get("DEMO_MODE_EFFECTIVE"))
    catalog = str(
        config.get("SA_CATALOG")
        or config.get("CATALOG")
        or ""
    ).strip()
    schema = str(
        config.get("SA_SCHEMA")
        or config.get("SCHEMA")
        or ""
    ).strip()
    warehouse = str(config.get("WAREHOUSE_ID") or "").strip()
    table_candidates = []
    for key in sorted(config):
        if key.endswith("_TABLE") and isinstance(config.get(key), str) and config.get(key):
            table_candidates.append(str(config[key]))
    if demo:
        detail = []
        if catalog:
            detail.append(f"catalog: {catalog}")
        if schema:
            detail.append(f"schema: {schema}")
        return {
            "mode": "demo",
            "connected": False,
            "workspace": "",
            "catalog": catalog,
            "schema": schema,
            "warehouse_id": warehouse,
            "tables": [],
            "headline": "DEMO — Databricks UC not connected",
            "detail": " · ".join(detail),
            "error": "",
        }

    try:
        ws = WorkspaceClient()
        workspace = str(getattr(getattr(ws, "config", None), "host", "") or "").rstrip("/")
        if not warehouse:
            try:
                warehouses = list(ws.warehouses.list())
            except Exception:
                warehouses = []
            for wh in warehouses:
                if getattr(wh, "warehouse_type", None) and "serverless" in str(wh.warehouse_type).lower():
                    warehouse = (getattr(wh, "id", "") or "").strip()
                    break
            if not warehouse:
                for wh in warehouses:
                    wid = (getattr(wh, "id", "") or "").strip()
                    if wid:
                        warehouse = wid
                        break
        if not warehouse:
            raise RuntimeError("No SQL warehouse available")
        probe_sql = "SELECT 1 AS ok"
        if catalog and schema:
            probe_sql = f"SHOW TABLES IN {catalog}.{schema}"
        resp = ws.statement_execution.execute_statement(
            warehouse_id=warehouse, statement=probe_sql, wait_timeout="10s"
        )
        status = getattr(resp, "status", None)
        if status is None or getattr(status, "state", None) != StatementState.SUCCEEDED:
            err = getattr(status, "error", None)
            msg = getattr(err, "message", "") if err is not None else ""
            raise RuntimeError(msg or "SQL execution failed")
        manifest = getattr(resp, "manifest", None)
        result = getattr(resp, "result", None)
        cols = [c.name for c in (getattr(getattr(manifest, "schema", None), "columns", None) or [])]
        rows = list(getattr(result, "data_array", None) or [])
        tables = []
        if probe_sql.startswith("SHOW TABLES IN") and rows:
            try:
                name_idx = cols.index("tableName")
            except ValueError:
                name_idx = -1
            if name_idx >= 0:
                tables = [str(r[name_idx]) for r in rows if len(r) > name_idx and r[name_idx]]
        headline = "Connected — Databricks UC reachable"
        parts = []
        if workspace:
            parts.append(f"workspace: {workspace.replace('https://', '')}")
        if catalog:
            parts.append(f"catalog: {catalog}")
        if schema:
            parts.append(f"schema: {schema}")
        if warehouse:
            parts.append(f"warehouse: {warehouse}")
        if tables:
            parts.append(f"tables: {len(tables)}")
        return {
            "mode": "live",
            "connected": True,
            "workspace": workspace,
            "catalog": catalog,
            "schema": schema,
            "warehouse_id": warehouse,
            "tables": tables,
            "headline": headline,
            "detail": " · ".join(parts),
            "error": "",
        }
    except Exception as e:  # noqa: BLE001 — UI helper; surface the reason to the page
        parts = []
        if catalog:
            parts.append(f"catalog: {catalog}")
        if schema:
            parts.append(f"schema: {schema}")
        if warehouse:
            parts.append(f"warehouse: {warehouse}")
        return {
            "mode": "live",
            "connected": False,
            "workspace": "",
            "catalog": catalog,
            "schema": schema,
            "warehouse_id": warehouse,
            "tables": [],
            "headline": "LIVE configured — Databricks UC unreachable",
            "detail": " · ".join(parts),
            "error": str(e),
        }


def _function_span(text: str, name: str) -> "tuple[int, int] | None":
    """1-based inclusive line span of the top-level function ``name``."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno or node.lineno
            return start, end
    return None


def replace_function(text: str, name: str, new_source: str) -> "tuple[str | None, str | None]":
    """Swap the top-level function ``name`` in ``text`` for ``new_source``.

    Returns ``(updated_text, error)``. The replacement is located by AST line
    span, not by string search, so an edit lands on exactly one function and
    cannot be confused by lookalike text elsewhere in the file.
    """
    try:
        tree = ast.parse(new_source)
    except SyntaxError as exc:
        return None, f"Syntax error at line {exc.lineno}: {exc.msg}"

    defined = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if len(defined) != 1 or defined[0].name != name:
        return None, (
            f"The edited text must define exactly one top-level function named `{name}`. "
            "The agent imports the tool by name, so a rename breaks the wiring."
        )

    span = _function_span(text, name)
    if span is None:
        return None, f"`{name}` is not defined at the top level of the file."

    start, end = span
    lines = text.splitlines()
    body = new_source.rstrip("\n").splitlines()
    updated_lines = lines[: start - 1] + body + lines[end:]
    updated = "\n".join(updated_lines) + ("\n" if text.endswith("\n") else "")

    try:
        ast.parse(updated)
    except SyntaxError as exc:
        return None, f"The edited file would not parse: line {exc.lineno}: {exc.msg}"

    return updated, None


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def tools_payload(request: Request) -> dict[str, Any]:
    """The JSON the inspector page (and any other consumer) fetches.

    ``tools`` is the registered surface in wiring order. Each item carries the
    source of the function that actually ran, the demo twin (if any), and the
    description + input schema the model was given.
    """
    llm_view: dict[str, dict[str, Any]] = {}
    ctx = getattr(request.app.state, "agent_context", None)
    for tool in list(getattr(ctx, "tools", None) or []):
        name = getattr(tool, "name", "") or ""
        if not name:
            continue
        llm_view[name] = {
            "description": getattr(tool, "description", "") or "",
            "schema": getattr(tool, "input_schema", None) or {},
        }

    inventory = _tool_functions(request)
    functions = inventory.functions
    demo_module = _demo_module()
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    file_cache: dict[str, str] = {}

    for fn in functions:
        name = getattr(fn, "__name__", "") or ""
        if not name:
            continue
        path = _source_file(fn)
        path_key = str(path) if path else ""
        if path_key and path_key not in file_cache:
            file_cache[path_key] = _file_text(path)
        file_text = file_cache.get(path_key, "")
        try:
            signature = str(inspect.signature(fn))
        except (TypeError, ValueError):
            signature = "(...)"
        source = _source(fn)
        if not source:
            warnings.append(f"Source unavailable for `{name}`.")
        span = _function_span(file_text, name) if file_text else None
        demo_fn = _demo_twin(fn, demo_module)
        items.append(
            {
                "name": name,
                "module": getattr(fn, "__module__", "") or "",
                "signature": signature,
                "docstring": inspect.getdoc(fn) or "",
                "source": source,
                "line": span[0] if span else _source_line(fn),
                "file": path_key or None,
                "file_writable": bool(path and os.access(path, os.W_OK)),
                "llm_description": (llm_view.get(name) or {}).get("description", ""),
                "llm_schema": (llm_view.get(name) or {}).get("schema", {}),
                "demo_name": getattr(demo_fn, "__name__", "") if demo_fn is not None else "",
                "demo_source": _source(demo_fn) if demo_fn is not None else "",
            }
        )

    if inventory.how == "module" and items:
        warnings.append(
            "The agent is not attached, so tool descriptions and input schemas "
            "were recovered from loaded modules instead of the running agent."
        )
    config = _runtime_config(functions)
    status = _context_status(request)
    connection = _connection_status(functions, config)
    for key in ("dev_ui_mount_error", "mcp_mount_error"):
        if status.get(key):
            warnings.append(f"`{key}`: {status[key]}")
    if not items:
        warnings.append("No tools found. Is the agent loaded in this runtime?")

    first_file = next((t["file"] for t in items if t.get("file")), None)
    return {
        "tools": items,
        "how": inventory.how,
        "config": config,
        "status": status,
        "connection": connection,
        "paths": {
            "primary_file": first_file,
            "files": sorted({t["file"] for t in items if t.get("file")}),
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


class ToolsSaveRequest(BaseModel):
    name: str
    source: str
    # The text the browser loaded. Guards against clobbering a concurrent edit;
    # optional so the endpoint is usable from curl.
    expected_original: str = ""


def _app_name() -> str:
    """The deployed app's name, for resolving its workspace source path.

    ``DATABRICKS_APP_NAME`` is injected by the Apps runtime and is the only
    value that matches what the Apps API expects.
    """
    return (os.environ.get("DATABRICKS_APP_NAME") or "").strip()


@dataclass(frozen=True)
class _WorkspaceUpload:
    """Result of mirroring an edit into the app's workspace source."""

    ok: bool
    path: str | None
    note: str


def _persist_to_workspace(request: Request, filename: str, text: str) -> _WorkspaceUpload:
    """Upload ``text`` next to the app's other source in the workspace.

    This is what makes an edit survive a restart: Apps rebuilds the container
    from this copy.
    """
    name = _app_name()
    if not name:
        return _WorkspaceUpload(
            False, None, "App name unknown (DATABRICKS_APP_NAME unset) — skipped the workspace copy."
        )
    try:
        ws = getattr(request.app.state, "workspace_client", None)
        if ws is None:
            from databricks.sdk import WorkspaceClient

            ws = WorkspaceClient()
        info = ws.apps.get(name)
        source_path = getattr(info, "default_source_code_path", None)
        if not source_path:
            return _WorkspaceUpload(
                False, None, f"App `{name}` reports no source code path — skipped the workspace copy."
            )
        target = source_path.rstrip("/") + f"/{filename}"
        ws.workspace.upload(target, io.BytesIO(text.encode("utf-8")), overwrite=True)
        return _WorkspaceUpload(True, target, "")
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller, never fatal
        return _WorkspaceUpload(False, None, f"Workspace copy failed ({type(exc).__name__}: {exc}).")


def _find_tool_fn(request: Request, name: str) -> Any | None:
    for fn in _tool_functions(request).functions:
        if getattr(fn, "__name__", "") == name:
            return fn
    return None


@dataclass(frozen=True)
class ToolSaveResult:
    """A save response: the JSON payload and the status to send with it."""

    payload: dict[str, Any]
    status: int


def save_tool_source(request: Request, body: ToolsSaveRequest) -> ToolSaveResult:
    fn = _find_tool_fn(request, body.name)
    if fn is None:
        return ToolSaveResult({"ok": False, "error": f"`{body.name}` is not a registered tool."}, 404)

    path = _source_file(fn)
    if path is None or not path.exists():
        return ToolSaveResult(
            {"ok": False, "error": f"Source file for `{body.name}` is not on disk in this runtime."}, 409
        )

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolSaveResult({"ok": False, "error": f"{path} is not readable: {exc}"}, 500)

    span = _function_span(text, body.name)
    if span is None:
        return ToolSaveResult({"ok": False, "error": f"`{body.name}` is not defined in {path}."}, 409)
    current = "\n".join(text.splitlines()[span[0] - 1 : span[1]])

    if body.expected_original.strip() and body.expected_original.strip() != current.strip():
        return ToolSaveResult(
            {
                "ok": False,
                "error": "The source file changed on disk since this page loaded. Reload and reapply the edit.",
            },
            409,
        )

    if body.source.strip() == current.strip():
        return ToolSaveResult({"ok": False, "error": "No changes to save."}, 400)

    updated, error = replace_function(text, body.name, body.source)
    if updated is None:
        return ToolSaveResult({"ok": False, "error": error}, 400)

    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ToolSaveResult({"ok": False, "error": f"Could not write {path}: {exc}"}, 500)
    # inspect.getsource reads through linecache; without this the page would
    # keep serving the pre-edit text for the rest of the process lifetime.
    linecache.checkcache(str(path))

    upload = _persist_to_workspace(request, path.name, updated)
    notes = [upload.note] if upload.note else []
    if upload.ok:
        notes.append(
            f"Wrote the app's workspace source at {upload.path}. "
            "Restart the app to load it; the next deploy rewrites this file from your repo."
        )
    else:
        notes.append(
            f"Wrote only the running container's copy at {path}. "
            "The container is rebuilt from workspace source on restart, so this edit will be lost — "
            "copy the text into the repo and redeploy to keep it."
        )

    return ToolSaveResult(
        {
            "ok": True,
            "tool": body.name,
            "durable": upload.ok,
            "container_path": str(path),
            "workspace_path": upload.path,
            "restart_required": True,
            "notes": notes,
        },
        200,
    )


@dataclass(frozen=True)
class ToolFile:
    """The file that defines a tool: its text and its basename."""

    contents: str | None
    filename: str | None


def tool_file_response(request: Request, name: str | None = None) -> ToolFile:
    """The file that defines ``name``, or the first file when it isn't given."""
    functions = _tool_functions(request).functions
    chosen = None
    if name:
        chosen = next((fn for fn in functions if getattr(fn, "__name__", "") == name), None)
    if chosen is None and functions:
        chosen = functions[0]
    if chosen is None:
        return ToolFile(None, None)
    path = _source_file(chosen)
    if path is None or not path.exists():
        return ToolFile(None, None)
    return ToolFile(path.read_text(encoding="utf-8"), path.name)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def _render_tools_ui() -> str:
    """The inspector page.

    The template is a plain string patched with two substitutions rather than
    an f-string: the page is mostly JavaScript, where ``{}`` and ``${}`` have
    their own meaning and would each need doubling. When the page is iframed
    (the unified shell's Tools tab, the Edit page's right panel) its own header
    and nav are hidden by the shared suppression script in
    ``_deploy_overlay_html`` (``window.self !== window.top``), so the host's
    chrome is never doubled up.
    """
    from ._ui_nav import _apx_nav_links, _deploy_overlay_html
    from ._ui_table import TABLE_CSS, TABLE_JS

    return (
        _PAGE.replace("{APX_TOOLS_NAVLINKS}", _apx_nav_links("tools"))
        .replace("{APX_TABLE_CSS}", TABLE_CSS)
        .replace("{APX_TABLE_JS}", TABLE_JS)
        .replace("{APX_TOOLS_OVERLAY}", _deploy_overlay_html())
    )


# Plain string, NOT an f-string — every brace and ${} below is literal JS.
_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tools — APX Dev</title>
<style>
  :root { --bg:#0a0a0a; --panel:#111; --border:#2a2a2a; --strong:#333;
          --text:#e5e7eb; --muted:#888; --dim:#555; --accent:#60b0ff;
          --accent-bg:#0d1f38; --accent-border:#1e3a5f;
          --ok:#4ade80; --warn:#fbbf24; --err:#f87171; color-scheme:dark; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:ui-sans-serif,system-ui,-apple-system,sans-serif; font-size:13px;
         background:var(--bg); color:var(--text); height:100vh; display:flex; flex-direction:column; }
  header { height:44px; flex-shrink:0; display:flex; align-items:center; gap:10px;
           padding:0 16px; background:var(--panel); border-bottom:1px solid var(--border); }
  .badge { background:var(--accent-bg); color:var(--accent); font-size:11px; font-weight:600;
           padding:2px 8px; border-radius:4px; letter-spacing:.5px; text-transform:uppercase; }
  header .title { font-weight:500; font-size:14px; color:var(--text); }
  header .file { font-family:ui-monospace,monospace; font-size:11px; color:var(--dim); }
  .spacer { margin-left:auto; }
  .link { color:var(--accent); text-decoration:none; font-size:12px; padding:4px 10px;
          border:1px solid var(--accent-border); border-radius:5px; background:var(--accent-bg); }
  .link:hover { background:#112a4a; }
  .btn { font:inherit; font-size:12px; color:var(--muted); background:transparent;
         border:1px solid var(--strong); border-radius:5px; padding:4px 11px; cursor:pointer; }
  .btn:hover { color:var(--text); border-color:var(--dim); }
  .btn-primary { color:var(--accent); background:var(--accent-bg); border-color:var(--accent-border); }
  .btn-primary:hover { border-color:var(--accent); color:var(--accent); }
  .btn:disabled { opacity:.45; cursor:default; }

  .wrap { flex:1; display:flex; min-height:0; }
  aside { width:290px; flex-shrink:0; border-right:1px solid var(--border);
          overflow-y:auto; background:#0b0b0b; }
  aside .hd { padding:10px 14px; font-size:10px; font-weight:700; color:var(--dim);
              text-transform:uppercase; letter-spacing:.06em; border-bottom:1px solid #161616; }
  .tool { display:block; width:100%; text-align:left; background:none; border:none;
          border-bottom:1px solid #161616; border-left:2px solid transparent;
          padding:10px 14px; cursor:pointer; color:var(--text); font:inherit; }
  .tool:hover { background:#141414; }
  .tool.active { background:var(--accent-bg); border-left-color:var(--accent); }
  .tool .n { font-family:ui-monospace,monospace; font-size:12.5px; font-weight:500; }
  .tool .m { font-size:10.5px; color:var(--dim); margin-top:3px; font-family:ui-monospace,monospace; }

  main { flex:1; overflow-y:auto; padding:22px 26px 60px; min-width:0; }
  .empty { color:var(--dim); font-style:italic; padding:28px 0; }

  h1 { font-size:17px; font-weight:600; font-family:ui-monospace,monospace; }
  .sig { margin-top:6px; font-family:ui-monospace,monospace; font-size:12px; color:var(--muted);
         word-break:break-all; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; }
  .chip { font-size:11px; font-family:ui-monospace,monospace; padding:2px 8px;
          border-radius:10px; border:1px solid var(--border); color:var(--muted); background:#0e0e0e; }
  .chip.ok { color:var(--ok); border-color:#14532d; background:#04140a; }
  .chip.warn { color:var(--warn); border-color:#5a3a00; background:#1a1200; }

  .bar { display:flex; align-items:center; gap:8px; margin:20px 0 8px; }
  .bar .lbl { font-size:10px; font-weight:700; color:var(--dim); text-transform:uppercase;
              letter-spacing:.06em; margin-right:auto; }
  .code { display:flex; background:#0d0d0d; border:1px solid #1e1e1e; border-radius:8px;
          overflow:auto; max-height:58vh; }
  .code .gutter { padding:10px 8px 10px 12px; text-align:right; color:#3a3a3a;
                  font-family:ui-monospace,monospace; font-size:11.5px; line-height:1.55;
                  user-select:none; white-space:pre; border-right:1px solid #1a1a1a; }
  .code pre { margin:0; padding:10px 14px; font-family:ui-monospace,monospace;
              font-size:11.5px; line-height:1.55; color:#c9d1d9; white-space:pre; }
  textarea { width:100%; min-height:58vh; background:#0d0d0d; border:1px solid var(--accent-border);
             border-radius:8px; padding:12px 14px; color:#c9d1d9; resize:vertical;
             font-family:ui-monospace,monospace; font-size:11.5px; line-height:1.55; }
  textarea:focus { outline:none; border-color:var(--accent); }
  textarea.run-args { min-height:7rem; border-color:var(--border); }
  #run-status { font-size:11.5px; font-family:ui-monospace,monospace; color:var(--muted); }
  #run-status.run { color:var(--accent); }
  #run-status.ok { color:var(--ok); }
  #run-status.err { color:var(--err); }
  #run-output { display:none; margin:12px 0 0; padding:12px 14px; background:#0b0b0b;
                border:1px solid #1c1c1c; border-radius:8px;
                font-family:ui-monospace,monospace; font-size:11.5px; line-height:1.55; color:#c9d1d9;
                white-space:pre-wrap; word-break:break-word; max-height:24rem; overflow:auto; }
  #run-output.show { display:block; }
  #run-output.err { color:#fda4af; }
  {APX_TABLE_CSS}

  details { margin-top:14px; border:1px solid #1c1c1c; border-radius:8px; background:#0d0d0d; }
  details > summary { cursor:pointer; padding:9px 13px; font-size:12px; color:var(--muted);
                      list-style:none; user-select:none; }
  details > summary::-webkit-details-marker { display:none; }
  details > summary::before { content:"▸ "; color:var(--dim); }
  details[open] > summary::before { content:"▾ "; }
  details .body { padding:0 13px 13px; }
  table.kv { width:100%; border-collapse:collapse; font-family:ui-monospace,monospace; font-size:11.5px; }
  table.kv td { padding:4px 8px 4px 0; vertical-align:top; border-bottom:1px solid #161616;
                word-break:break-all; }
  table.kv td:first-child { color:var(--muted); width:210px; white-space:nowrap; }

  #banner { margin:0 26px; margin-top:16px; padding:11px 15px; border-radius:8px;
            font-size:12.5px; line-height:1.5; display:none; }
  #banner.show { display:block; }
  #banner.ok { background:#04140a; border:1px solid #14532d; color:#a7f3c9; }
  #banner.warn { background:#1a1200; border:1px solid #5a3a00; color:#ffd080; }
  #banner.err { background:#2a0f0f; border:1px solid #7f1d1d; color:#fda4af; }
  #banner ul { margin:6px 0 0 18px; }
  #banner code { font-family:ui-monospace,monospace; font-size:11.5px; }

  header nav { margin-left:auto; display:flex; gap:4px; }
  header nav a { font-size:12px; color:#888; text-decoration:none; padding:3px 10px;
                 border-radius:5px; border:1px solid transparent; }
  header nav a:hover { color:#ccc; border-color:#333; }
  header nav a.active { color:#60b0ff; background:#0d1f38; border-color:#1e3a5f; }
</style>
</head>
<body>
<header>
  <span class="badge">Tools</span>
  <span class="title" id="hdr-title">Tool inspector</span>
  <span class="file" id="hdr-file"></span>
  <nav>{APX_TOOLS_NAVLINKS}</nav>
  <button class="btn" id="btn-deploy">Deploy ▶</button>
  <button class="btn" id="btn-reload">Reload</button>
</header>

<div id="banner"></div>

<div class="wrap">
  <aside>
    <div class="hd" id="nav-hd">Loading…</div>
    <div id="nav"></div>
  </aside>
  <main id="main"><div class="empty">Loading tool logic…</div></main>
</div>

<script>
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));

let DATA = null;
let current = null;
let editing = false;

// ── Live Run console (POST /_apx/replay/tool — the same route chat uses) ──
const RUN_ARGS = {};

function argsSkeleton(tool) {
  const props = (tool.llm_schema && tool.llm_schema.properties) || {};
  const out = {};
  Object.keys(props).forEach((k) => {
    const p = props[k] || {};
    if (typeof p.default !== 'undefined' && p.default !== '' && p.default !== null) out[k] = p.default;
    else if (p.type === 'string') out[k] = '';
    else if (p.type === 'number' || p.type === 'integer') out[k] = 0;
    else if (p.type === 'boolean') out[k] = false;
    else if (p.type === 'array') out[k] = [];
    else if (p.type === 'object') out[k] = {};
    else out[k] = '';
  });
  return out;
}

function prettyOutput(raw) {
  const text = String(raw ?? '');
  if (!text) return '';
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch (e) {
    return text;
  }
}

{APX_TABLE_JS}

async function runCurrent() {
  const btn = document.getElementById('btn-run');
  const status = document.getElementById('run-status');
  const out = document.getElementById('run-output');
  const argsEl = document.getElementById('run-args');
  if (!btn || !out || !argsEl || !current) return;

  let args;
  const text = argsEl.value.trim();
  if (!text) {
    args = {};
  } else {
    try {
      args = JSON.parse(text);
    } catch (e) {
      status.className = 'err';
      status.textContent = 'Args are not valid JSON: ' + e.message;
      return;
    }
    if (args === null || Array.isArray(args) || typeof args !== 'object') {
      status.className = 'err';
      status.textContent = 'Args must be a JSON object — empty is allowed.';
      return;
    }
  }

  btn.disabled = true;
  btn.textContent = 'Running…';
  status.className = 'run';
  status.textContent = 'POST /_apx/replay/tool → ' + current.name;
  out.className = '';
  out.textContent = '';

  try {
    const r = await (window.apxDevFetch || fetch)('/_apx/replay/tool', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool_name: current.name, args }),
    });
    const raw = await r.text();
    let data = null;
    try { data = JSON.parse(raw); } catch (e) { data = null; }
    if (!r.ok) {
      status.className = 'err';
      status.textContent = 'HTTP ' + r.status +
        (data && data.error ? ' — ' + data.error : '');
      out.textContent = data ? '' : raw.slice(0, 400);
      out.classList.add('show');
      out.classList.add('err');
      return;
    }
    if (data && data.ok) {
      status.className = 'ok';
      status.textContent = 'ok · ' +
        (typeof data.duration_ms === 'number' ? data.duration_ms + ' ms' : 'done');
      out.innerHTML = renderToolOutput(data.output);
      out.classList.add('show');
      out.classList.remove('err');
      return;
    }
    status.className = 'err';
    status.textContent = data && data.error
      ? 'Tool error: ' + data.error
      : 'Unexpected response: ' + raw.slice(0, 200);
    out.textContent = '';
    out.classList.remove('show');
  } catch (e) {
    status.className = 'err';
    status.textContent = 'Run failed: ' + (e && e.message ? e.message : String(e));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run ▶';
  }
}

function banner(kind, html) {
  const el = document.getElementById('banner');
  el.className = 'show ' + kind;
  el.innerHTML = html;
}
function clearBanner() {
  const el = document.getElementById('banner');
  el.className = '';
  el.innerHTML = '';
}
function loadFailure(message, raw) {
  banner('err', esc(message) + ' — <a href="/_apx/tools" target="_blank" rel="noopener">open the inspector in a new tab</a>.');
  const sample = String(raw || '').trim();
  document.getElementById('main').innerHTML =
    '<div class="empty"><strong>Tool logic did not load.</strong>' +
    '<div style="margin-top:10px;color:#888;line-height:1.6;max-width:72ch">' + esc(message) +
    '<br><br>If this page is inside the console iframe, the browser may have routed the fetch through the Databricks app login instead of returning JSON.' +
    '</div>' +
    (sample ? '<pre style="margin-top:12px;text-align:left;white-space:pre-wrap;background:#0f0f0f;border:1px solid #222;border-radius:8px;padding:12px;max-width:100%;overflow:auto">' + esc(sample.slice(0, 320)) + '</pre>' : '') +
    '</div>';
}

async function load(selectName) {
  const req = window.apxDevFetch || fetch;
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), 8000) : null;
  try {
    const r = await req('/_apx/tools/list', Object.assign({ cache: 'no-store' }, ctl ? { signal: ctl.signal } : {}));
    const raw = await r.text();
    if (!r.ok) {
      loadFailure('Could not read the tool list: HTTP ' + r.status, raw);
      return;
    }
    let parsed = null;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      loadFailure('The tool list did not return JSON.', raw);
      return;
    }
    DATA = parsed;
    renderConnectionBanner();
    if (!current || !DATA.tools.some((t) => t.name === current.name)) {
      current = DATA.tools.length ? DATA.tools[0] : null;
    } else {
      current = DATA.tools.find((t) => t.name === current.name);
    }
    if (selectName) current = DATA.tools.find((t) => t.name === selectName) || current;

    document.getElementById('hdr-title').textContent =
      DATA.tools.length + ' tool' + (DATA.tools.length === 1 ? '' : 's');
    document.getElementById('hdr-file').textContent =
      (DATA.paths.files && DATA.paths.files.length)
        ? DATA.paths.files.length + ' file' + (DATA.paths.files.length === 1 ? '' : 's')
        : 'no source file on disk';
    document.getElementById('nav-hd').textContent =
      DATA.how === 'agent' ? 'Registered with the agent' : 'Recovered from loaded modules';
    renderNav();
    renderDetail();
  } catch (e) {
    const msg = e && e.name === 'AbortError' ? 'Tool loading timed out.' : 'Tool loading failed: ' + (e && e.message ? e.message : String(e));
    loadFailure(msg, '');
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function renderConnectionBanner() {
  const c = (DATA && DATA.connection) || null;
  if (!c) {
    clearBanner();
    return;
  }
  const kind = c.connected ? 'ok' : (c.mode === 'demo' ? 'warn' : 'err');
  const bits = [];
  if (c.detail) bits.push(esc(c.detail));
  if (c.error) bits.push('reason: ' + esc(c.error));
  banner(kind, '<strong>' + esc(c.headline || 'Connection status unavailable') + '</strong>' +
    (bits.length ? '<div style="margin-top:4px">' + bits.join('<br>') + '</div>' : ''));
}


function renderNav() {
  const nav = document.getElementById('nav');
  nav.innerHTML = DATA.tools.map((t) => {
    const params = (t.signature.match(/,/g) || []).length;
    return '<button class="tool' + (current && t.name === current.name ? ' active' : '') +
      '" data-name="' + esc(t.name) + '">' +
      '<div class="n">' + esc(t.name) + '</div>' +
      '<div class="m">' + esc(t.module) +
      (t.line ? ' · line ' + t.line : '') + ' · ' +
      (params + 1) + ' param' + (params === 0 ? '' : 's') + '</div>' +
      '</button>';
  }).join('');
  nav.querySelectorAll('.tool').forEach((b) => {
    b.addEventListener('click', () => {
      current = DATA.tools.find((t) => t.name === b.dataset.name);
      editing = false;
      clearBanner();
      renderNav();
      renderDetail();
    });
  });
}

function codeBlock(text, startLine) {
  const lines = String(text || '').replace(/\n$/, '').split('\n');
  const gutter = lines.map((_, i) => (startLine || 1) + i).join('\n');
  return '<div class="code"><div class="gutter">' + esc(gutter) + '</div>' +
    '<pre>' + esc(lines.join('\n')) + '</pre></div>';
}

function renderDetail() {
  const main = document.getElementById('main');
  if (!current) {
    main.innerHTML = '<div class="empty">No tools registered in this runtime.</div>';
    return;
  }
  const t = current;
  const demo = DATA.config.DEMO_MODE_EFFECTIVE;
  const parts = [];

  parts.push('<h1>' + esc(t.name) + '</h1>');
  parts.push('<div class="sig">def ' + esc(t.name) + esc(t.signature) + '</div>');

  const conn = DATA.connection || {};
  const connText = conn.connected
    ? 'connected · ' + [conn.workspace && conn.workspace.replace('https://', ''), conn.catalog, conn.schema].filter(Boolean).join(' · ')
    : (conn.mode === 'demo'
        ? 'DEMO — Databricks UC not connected'
        : 'LIVE configured — Databricks UC unreachable');
  parts.push('<div class="chips">' +
    '<span class="chip ' + (conn.connected ? 'ok' : (conn.mode === 'demo' ? 'warn' : 'warn')) + '">' + esc(connText) + '</span>' +
    '<span class="chip">' + esc(DATA.how === 'agent' ? 'registered' : 'recovered from source') + '</span>' +
    (t.demo_name ? '<span class="chip">demo twin: ' + esc(t.demo_name) + '</span>' : '') +
    '</div>');

  parts.push('<div class="bar"><span class="lbl">Source — ' + esc(t.module) +
    (t.line ? ':' + t.line : '') + '</span>' +
    '<button class="btn" id="btn-copy">Copy</button>' +
    '<button class="btn btn-primary" id="btn-edit">Edit</button></div>');

  if (editing) {
    parts.push('<textarea id="editor" spellcheck="false"></textarea>');
    parts.push('<div class="bar" style="margin-top:10px">' +
      '<span class="lbl">Editing rewrites the deployed function; the repo stays the source of truth</span>' +
      '<button class="btn" id="btn-cancel">Cancel</button>' +
      '<button class="btn btn-primary" id="btn-save">Save</button></div>');
  } else {
    parts.push(codeBlock(t.source, t.line));
  }

  // Live Run console — the same tool-execution route the chat uses.
  if (RUN_ARGS[t.name] === undefined) {
    let skeleton = '';
    try {
      skeleton = JSON.stringify(argsSkeleton(t), null, 2);
    } catch (e) {
      skeleton = '';
    }
    RUN_ARGS[t.name] = skeleton === '{}' ? '' : skeleton;
  }
  parts.push('<details open><summary>Run this tool</summary><div class="body">' +
    '<p style="color:#666;font-size:11.5px;line-height:1.6">Calls the <strong>registered</strong>' +
    ' function right now via <code>POST /_apx/replay/tool</code> — unsaved edits are not run.' +
    ' Args are a JSON object; empty is allowed. Runs on <code>Ctrl/⌘+Enter</code>.</p>' +
    '<textarea id="run-args" class="run-args" spellcheck="false"' +
    ' placeholder="{ &quot;key&quot;: &quot;value&quot; }"></textarea>' +
    '<div class="bar" style="margin-top:10px">' +
    '<span id="run-status">Not run yet</span>' +
    '<button class="btn btn-primary" id="btn-run">Run ▶</button></div>' +
    '<div id="run-output"></div>' +
    '</div></details>');

  if (t.source.indexOf('_demo_mode()') !== -1) {
    parts.push('<details><summary>Both branches — this function short-circuits on <code>_demo_mode()</code></summary>' +
      '<div class="body"><p style="color:#888;font-size:12px;line-height:1.6">' +
      'This deployment is currently using the ' +
      '<strong>' + (demo ? 'synthetic (demo)' : 'live UC/SQL') + '</strong> branch.' +
      (demo ? '' : ' The tables it reads are shown in Runtime below.') +
      '</p></div></details>');
  }

  if (t.llm_description || Object.keys(t.llm_schema || {}).length) {
    parts.push('<details><summary>What the model is told</summary><div class="body">' +
      (t.llm_description
        ? '<p style="color:#c9d1d9;font-size:12px;line-height:1.6;white-space:pre-wrap">' +
          esc(t.llm_description) + '</p>'
        : '<p style="color:#666;font-size:12px">No description available from the running agent.</p>') +
      (Object.keys(t.llm_schema || {}).length
        ? '<div class="sig" style="margin-top:10px">input schema</div>' +
          codeBlock(JSON.stringify(t.llm_schema, null, 2), 1)
        : '') +
      '</div></details>');
  }

  if (t.demo_source) {
    parts.push('<details><summary>' + esc(t.demo_name) + ' — ' +
      (demo ? 'the branch this deployment runs' : 'not executed in this deployment') +
      '</summary><div class="body">' + codeBlock(t.demo_source, 1) + '</div></details>');
  }

  const cfg = Object.entries(DATA.config || {})
    .filter(([k]) => k !== 'DEMO_MODE' && k !== 'DEMO_MODE_EFFECTIVE')
    .map(([k, v]) => '<tr><td>' + esc(k) + '</td><td>' + esc(v) + '</td></tr>').join('');
  parts.push('<details><summary>Runtime — what this process is actually wired to</summary><div class="body">' +
    '<table class="kv">' + cfg + '</table>' +
    '<table class="kv" style="margin-top:12px">' +
    '<tr><td>source file</td><td>' + esc(t.file || '—') +
      (t.file_writable ? ' <span style="color:#4ade80">writable</span>'
                       : ' <span style="color:#f87171">read-only</span>') + '</td></tr>' +
    '<tr><td>agent context</td><td>' + (DATA.status.agent_context ? 'populated' : 'MISSING') + '</td></tr>' +
    '<tr><td>model-facing tools</td><td>' + DATA.status.llm_tool_count + '</td></tr>' +
    '</table>' +
    (DATA.warnings.length
      ? '<div style="margin-top:12px;color:#ffd080;font-size:12px;line-height:1.6">' +
        DATA.warnings.map((w) => '⚠ ' + esc(w)).join('<br>') + '</div>'
      : '') +
    '</div></details>');

  parts.push('<div class="bar" style="margin-top:16px"><span class="lbl"></span>' +
    '<a class="btn" href="/_apx/tools/file?name=' + encodeURIComponent(t.name) +
    '" download="' + esc(t.file ? t.file.split('/').pop() : 'tools.py') + '">' +
    'Download ' + esc(t.file ? t.file.split('/').pop() : 'tools.py') + '</a></div>');

  main.innerHTML = parts.join('');
  wireDetail();
}

function wireDetail() {
  const copyBtn = document.getElementById('btn-copy');
  if (copyBtn) copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(current.source);
      banner('ok', 'Copied <code>' + esc(current.name) + '</code> to the clipboard.');
    } catch (e) {
      banner('err', 'Clipboard blocked by the browser: ' + esc(e.message));
    }
  });

  const editBtn = document.getElementById('btn-edit');
  if (editBtn) editBtn.addEventListener('click', () => {
    editing = true;
    clearBanner();
    renderDetail();
  });

  const editor = document.getElementById('editor');
  if (editor) {
    editor.value = current.source;
    editor.focus();
  }

  const cancel = document.getElementById('btn-cancel');
  if (cancel) cancel.addEventListener('click', () => { editing = false; renderDetail(); });

  const runBtn = document.getElementById('btn-run');
  if (runBtn) runBtn.addEventListener('click', runCurrent);

  const runArgs = document.getElementById('run-args');
  if (runArgs) {
    runArgs.value = (RUN_ARGS[current.name] !== undefined) ? RUN_ARGS[current.name] : '';
    runArgs.addEventListener('input', () => { RUN_ARGS[current.name] = runArgs.value; });
    runArgs.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        runCurrent();
      }
    });
  }

  const save = document.getElementById('btn-save');
  if (save) save.addEventListener('click', doSave);
}

async function doSave() {
  const btn = document.getElementById('btn-save');
  const editor = document.getElementById('editor');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const r = await (window.apxDevFetch || fetch)('/_apx/tools/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: current.name,
        source: editor.value,
        expected_original: current.source,
      }),
    });
    const data = await r.json();
    if (!data.ok) {
      banner('err', '<strong>Not saved.</strong> ' + esc(data.error));
      btn.disabled = false;
      btn.textContent = 'Save';
      return;
    }
    editing = false;
    const notes = (data.notes || []).map((n) => '<li>' + esc(n) + '</li>').join('');
    banner(data.durable ? 'ok' : 'warn',
      '<strong>' + (data.durable ? 'Saved — durable' : 'Saved to the container only') + '</strong>' +
      '<ul>' + notes + '</ul>');
    await load(current.name);
  } catch (e) {
    banner('err', 'Save failed: ' + esc(e.message));
    btn.disabled = false;
    btn.textContent = 'Save';
  }
}

document.getElementById('btn-reload').addEventListener('click', () => {
  editing = false;
  clearBanner();
  load();
});

load();
</script>
{APX_TOOLS_OVERLAY}
</body>
</html>
"""
