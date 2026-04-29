"""Dev UI — /_apx/edit in-browser agent_router.py editor and source manipulation utilities."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ._models import AgentContext
from ._ui_nav import _apx_nav_css, _apx_nav_html, _deploy_overlay_html

# ---------------------------------------------------------------------------
# /_apx/edit — in-browser agent_router.py editor
# ---------------------------------------------------------------------------


def _find_agent_router_path() -> "Path | None":
    """Return the path to agent_router.py by scanning loaded modules."""
    import sys
    from pathlib import Path

    for name, mod in sys.modules.items():
        if name.endswith(".backend.agent_router"):
            origin = getattr(mod, "__file__", None)
            if origin:
                return Path(origin)
    return None


def _build_tool_function(
    name: str,
    description: str,
    params: list[dict[str, Any]],
    returns: str,
    ws_type: str,
    body: str | None = None,
) -> str:
    """Generate Python source for a new agent tool function."""
    import re as _re

    name = _re.sub(r"\W", "_", name) or "my_tool"
    description = description or "Describe what this tool does."

    sig_params = ", ".join(f"{p['name']}: {p['type']}" for p in params if p.get("name"))
    sep = ", " if sig_params else ""
    sig = f"def {name}({sig_params}{sep}ws: {ws_type}) -> {returns}:"

    doc_lines = [description]
    for p in params:
        if p.get("name") and p.get("desc"):
            doc_lines.append(f"    {p['name']}: {p['desc']}")

    if len(doc_lines) > 1:
        docstring = '    """' + doc_lines[0] + "\n" + "\n".join(doc_lines[1:]) + '\n    """'
    else:
        docstring = '    """' + doc_lines[0] + '"""'

    if body:
        # Normalise indentation — ensure every non-empty line has at least 4 spaces
        body_lines = []
        for line in body.splitlines():
            if line.strip() and not line.startswith("    "):
                line = "    " + line.lstrip()
            body_lines.append(line)
        body_code = "\n".join(body_lines)
    else:
        body_code = "    # TODO: implement your tool\n    pass"

    return f"{sig}\n{docstring}\n{body_code}"


def _find_deploy_root() -> "Path | None":
    """Walk up from agent_router.py to find the directory containing pyproject.toml."""
    ar = _find_agent_router_path()
    if ar is None:
        return None
    p = ar.parent
    for _ in range(6):
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return None


def _find_evals_path() -> "Path | None":
    """Return the path to evals.json colocated with agent_router.py."""
    ar = _find_agent_router_path()
    return ar.parent / "evals.json" if ar is not None else None


# ---------------------------------------------------------------------------
# Setup wizard helpers
# ---------------------------------------------------------------------------


def _find_env_path() -> "Path | None":
    """Return the .env file in the project root (creates it if absent)."""
    root = _find_deploy_root()
    if root is None:
        return None
    return root / ".env"


def _read_env_file(path: "Path") -> "dict[str, str]":
    """Parse a .env file into a dict, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_env_file(path: "Path", updates: "dict[str, str]") -> None:
    """Merge updates into an existing .env file, preserving comments and other vars."""
    lines: list[str] = []
    written: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    written.add(key)
                    continue
            lines.append(line)
    for k, v in updates.items():
        if k not in written:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")



def _splice_tool(source: str, fn_code: str, fn_name: str) -> str:
    """Insert fn_code before the top-level agent assignment and add fn_name to the LlmAgent tools list.

    Handles any agent wrapper: Agent/LlmAgent, LoopAgent, SequentialAgent, ParallelAgent, etc.
    """
    import re as _re

    stub = "\n\n" + fn_code

    # Find insertion point: last `agent = <anything>(` — works with LoopAgent, SequentialAgent, etc.
    insert_at = -1
    for m in _re.finditer(r'\nagent\s*=\s*\w+\s*\(', source):
        insert_at = m.start()
    result = (source[:insert_at] + stub + source[insert_at:]) if insert_at != -1 else (source + stub)

    # Add fn_name to the last `Agent(tools=[` list — handles both flat and wrapped patterns.
    # rfind picks the innermost LlmAgent when multiple Agent() calls exist.
    tools_pos = result.rfind("Agent(tools=[")
    if tools_pos != -1:
        close_bracket = result.find("])", tools_pos)
        if close_bracket != -1:
            inside = result[tools_pos:close_bracket]
            sep = "" if inside.rstrip().endswith("[") else ", "
            result = result[:close_bracket] + sep + fn_name + result[close_bracket:]

    return result


def _fix_sql_identifiers(body: str, tables: "dict[str, list[str]]") -> str:
    """Fix hallucinated SQL table/column names in an LLM-generated function body.

    Uses fuzzy matching (difflib) against the known schema so the fix is
    deterministic — no second LLM call required.
    """
    import re as _re
    from difflib import get_close_matches as _gcm

    if not tables:
        return body

    known_tables = list(tables.keys())
    known_tables_lower = {t.lower(): t for t in known_tables}

    # Build a flat column → canonical-name map across all tables
    known_cols: dict[str, str] = {}
    for cols in tables.values():
        for entry in cols:
            col = entry.split("(")[0]
            known_cols[col.lower()] = col

    def _nearest_table(name: str) -> str:
        lo = name.lower()
        if lo in known_tables_lower:
            return known_tables_lower[lo]
        hits = _gcm(lo, list(known_tables_lower.keys()), n=1, cutoff=0.4)
        return known_tables_lower[hits[0]] if hits else name

    def _nearest_col(name: str) -> str:
        lo = name.lower()
        if lo in known_cols:
            return known_cols[lo]
        hits = _gcm(lo, list(known_cols.keys()), n=1, cutoff=0.6)
        return known_cols[hits[0]] if hits else name

    # Fix table names after FROM / JOIN
    body = _re.sub(
        r'\b(FROM|JOIN)\s+(\w+)',
        lambda m: f"{m.group(1)} {_nearest_table(m.group(2))}",
        body,
        flags=_re.IGNORECASE,
    )

    # Fix column names inside SQL string literals (single or triple-quoted)
    # Strategy: find every word boundary identifier inside an SQL-ish string and
    # replace only when there's a close fuzzy match to a known column (cutoff=0.6
    # means we won't corrupt Python variable names or SQL keywords).
    sql_keywords = {
        "select", "from", "where", "and", "or", "not", "in", "between",
        "order", "by", "group", "limit", "join", "on", "as", "distinct",
        "count", "sum", "avg", "max", "min", "case", "when", "then", "else",
        "end", "null", "is", "like", "having", "inner", "left", "right",
        "outer", "with", "insert", "update", "delete", "set",
    }

    def _fix_col_in_sql(m: Any) -> str:
        word = m.group(0)
        if word.lower() in sql_keywords:
            return word
        # Don't touch identifiers that are already valid table names
        if word.lower() in known_tables_lower:
            return word
        return _nearest_col(word)

    # Only patch identifiers that appear inside SQL string fragments in the body
    def _patch_sql_string(m: Any) -> str:
        return _re.sub(r'\b([A-Za-z_]\w*)\b', _fix_col_in_sql, m.group(0))

    body = _re.sub(r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"[^"]*"|\'[^\']*\')', _patch_sql_string, body)

    return body


def _mine_schema_from_source(source: str) -> "dict[str, list[str]]":
    """Fallback schema: mine table/column names from existing SQL in agent_router.py.

    Used when Unity Catalog is unreachable (local dev). Searches only inside
    string literals that look like SQL (contain SELECT/FROM keywords), so prose
    in docstrings and comments doesn't pollute the result.
    """
    import re as _re

    _SQL_KW = {
        "select", "from", "where", "and", "or", "not", "in", "between",
        "order", "by", "group", "limit", "join", "on", "as", "distinct",
        "count", "sum", "avg", "max", "min", "case", "when", "then", "else",
        "end", "null", "is", "like", "having", "inner", "left", "right",
        "outer", "with", "insert", "update", "delete", "set",
    }

    # Extract all string literal contents (triple-quoted then double-quoted)
    sql_fragments: list[str] = []
    for pattern in (r'"""([\s\S]*?)"""', r"'''([\s\S]*?)'''", r'"([^"]*)"', r"'([^']*)'"):
        for m in _re.finditer(pattern, source):
            text = m.group(1)
            # Only keep fragments that look like SQL
            if _re.search(r'\bSELECT\b', text, _re.IGNORECASE) or \
               _re.search(r'\bFROM\b.*\bWHERE\b', text, _re.IGNORECASE):
                sql_fragments.append(text)

    tables: dict[str, set[str]] = {}
    for sql in sql_fragments:
        # FROM / JOIN → table name
        for m in _re.finditer(r'\b(?:FROM|JOIN)\s+(\w+)', sql, _re.IGNORECASE):
            name = m.group(1)
            if name.lower() not in _SQL_KW and len(name) > 2:
                tables.setdefault(name, set())

        # SELECT col, col FROM table → associate columns
        for m in _re.finditer(
            r'SELECT\s+([\w\s,\*]+?)\s+FROM\s+(\w+)',
            sql, _re.IGNORECASE | _re.DOTALL,
        ):
            cols_str, table = m.group(1).strip(), m.group(2)
            if table.lower() in _SQL_KW or cols_str.strip() == "*":
                continue
            tables.setdefault(table, set())
            for col in cols_str.split(","):
                col = col.strip()
                if col and col.lower() not in _SQL_KW and " " not in col:
                    tables[table].add(f"{col}(UNKNOWN)")

        # WHERE col = ... → column hint
        for m in _re.finditer(r'\bWHERE\s+(\w+)\s*=', sql, _re.IGNORECASE):
            col = m.group(1)
            if col.lower() not in _SQL_KW:
                for t in tables:
                    tables[t].add(f"{col}(UNKNOWN)")

    return {t: sorted(cols) for t, cols in tables.items() if t}


def _remove_tool(source: str, fn_name: str) -> str:
    """Remove a tool function definition and its entry from Agent(tools=[...])."""
    import re as _re

    # Find the function definition start
    fn_start = _re.search(rf'^def {_re.escape(fn_name)}\b', source, _re.MULTILINE)
    if not fn_start:
        return source

    # Find the end: next top-level def/class/agent= line
    rest = source[fn_start.start():]
    fn_end_m = _re.search(r'\n(?=(?:def |class |agent\s*=))', rest)
    fn_end = fn_start.start() + (fn_end_m.start() + 1 if fn_end_m else len(rest))

    # Strip any blank lines immediately before the def
    before = source[:fn_start.start()].rstrip("\n")
    after = source[fn_end:]
    result = before + "\n\n" + after

    # Remove from tools list — handles both `name` and `, name` forms
    result = _re.sub(rf',?\s*\b{_re.escape(fn_name)}\b\s*,?', _clean_tools_list_comma, result)

    return result


def _clean_tools_list_comma(m: "re.Match[str]") -> str:  # type: ignore[name-defined]
    """Collapse double-commas / trailing commas left after removing a tools entry."""
    txt = m.group(0)
    # If we removed 'name,' leave the preceding comma; if ', name' leave nothing
    if txt.startswith(",") and txt.endswith(","):
        return ","  # was ", name,"  → collapse to ","
    return ""  # was "name," or ", name" → remove entirely


def _extract_schemas_from_source(source: str) -> list[dict[str, Any]]:
    """AST-parse Python source and extract tool schemas without executing it.

    Returns the same shape as OpenAI function-calling schemas so the preview
    panel shows exactly what the model will receive.
    """
    import ast

    _TYPE_MAP = {
        "str": "string", "int": "integer", "float": "number",
        "bool": "boolean", "list": "array", "dict": "object",
        "List": "array", "Dict": "object", "Any": "string",
    }

    def py_json_type(node: Any) -> str:
        if node is None:
            return "string"
        s = ast.unparse(node)
        base = s.split("[")[0].strip()
        return _TYPE_MAP.get(base, "string")

    def parse_param_descs(doc: str) -> dict[str, str]:
        descs: dict[str, str] = {}
        for line in doc.splitlines():
            line = line.strip()
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip()
                if k and " " not in k and not k.startswith(">>>"):
                    descs[k] = v.strip()
        return descs

    _INJECTED_NAMES = {"ws", "headers", "ctx", "request", "db", "session"}
    _INJECTED_TYPES = {
        "AppClient", "UserClient", "Client", "Headers",
        "Request", "Dependencies", "AgentDependency",
    }

    def is_injected(arg: Any) -> bool:
        if arg.arg in _INJECTED_NAMES:
            return True
        if arg.annotation:
            ann = ast.unparse(arg.annotation)
            return any(t in ann for t in _INJECTED_TYPES)
        return False

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [{"_error": f"Syntax error at line {e.lineno}: {e.msg}"}]

    schemas = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("_"):
            continue

        doc = ast.get_docstring(node) or ""
        description = doc.split("\n")[0].strip() if doc else ""
        param_descs = parse_param_descs(doc)

        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in node.args.args:
            if arg.arg == "self" or is_injected(arg):
                continue
            prop: dict[str, Any] = {"type": py_json_type(arg.annotation)}
            if arg.arg in param_descs:
                prop["description"] = param_descs[arg.arg]
            properties[arg.arg] = prop
            required.append(arg.arg)

        # Params with defaults are optional
        n_defaults = len(node.args.defaults)
        if n_defaults:
            optional = {a.arg for a in node.args.args[-n_defaults:]}
            required = [r for r in required if r not in optional]

        schemas.append({
            "name": node.name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                **({"required": required} if required else {}),
            },
        })

    return schemas


def _parse_agent_nodes(source: str) -> list[dict[str, Any]]:
    """AST-parse agent_router.py and return all Agent/LlmAgent variable assignments.

    Returns list of {name, tools, instructions} dicts — one per Agent(...) call
    that is directly assigned to a name (e.g. ``data_agent = Agent(tools=[...])``,
    including the main ``agent = Agent(...)``).  Wrapped patterns such as
    ``agent = LoopAgent(Agent(...))`` are detected: the inner Agent node is
    returned with name "agent" and wrapper recorded as ``wrapper``.
    """
    import ast as _ast

    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return []

    nodes: list[dict[str, Any]] = []

    def _extract_agent_call(call_node: Any) -> dict[str, Any] | None:
        """Return {tools, instructions} from an Agent/LlmAgent Call node, or None."""
        if not isinstance(call_node, _ast.Call):
            return None
        func_name = ""
        if isinstance(call_node.func, _ast.Name):
            func_name = call_node.func.id
        elif isinstance(call_node.func, _ast.Attribute):
            func_name = call_node.func.attr
        if func_name not in ("Agent", "LlmAgent"):
            return None
        tools: list[str] = []
        instructions: str = ""
        for kw in call_node.keywords:
            if kw.arg == "tools" and isinstance(kw.value, _ast.List):
                tools = [e.id for e in kw.value.elts if isinstance(e, _ast.Name)]
            elif kw.arg == "instructions" and isinstance(kw.value, _ast.Constant):
                instructions = str(kw.value.value)
        return {"tools": tools, "instructions": instructions}

    for stmt in tree.body:
        if not isinstance(stmt, _ast.Assign):
            continue
        if not (stmt.targets and isinstance(stmt.targets[0], _ast.Name)):
            continue
        var_name = stmt.targets[0].id
        val = stmt.value

        # Direct: data_agent = Agent(tools=[...])
        direct = _extract_agent_call(val)
        if direct is not None:
            nodes.append({"name": var_name, "wrapper": None, **direct})
            continue

        # Wrapped: agent = LoopAgent(Agent(tools=[...]), ...)
        if isinstance(val, _ast.Call) and isinstance(val.func, _ast.Name):
            wrapper_name = val.func.id
            for arg in val.args:
                inner = _extract_agent_call(arg)
                if inner is not None:
                    nodes.append({"name": var_name, "wrapper": wrapper_name, **inner})
                    break

    return nodes


def _render_edit_ui(content: str, not_found: bool = False) -> str:
    """Return a split-panel authoring page: CodeMirror left, schema preview right.

    Left panel — editable agent_router.py with Python syntax highlighting.
    Right panel — live tool schemas (what the model sees) updated on debounce.
    New Tool modal — structured form that generates correct function boilerplate.

    Cmd/Ctrl+S saves. APX dev server hot-reloads on file change.
    """
    import json as _json
    import re as _re

    content_js = _json.dumps(content)
    # Detect the AppClient alias (e.g. "Client = Dependencies.Client" → "Client")
    _alias_m = _re.search(r"^(\w+)\s*=\s*Dependencies\.Client", content, _re.MULTILINE)
    ws_type = _alias_m.group(1) if _alias_m else "AppClient"
    ws_type_js = _json.dumps(ws_type)
    not_found_banner = (
        '<div id="apx-banner"><strong>⚠ agent_router.py not found</strong> — '
        "the file could not be located in the running process.</div>"
        if not_found
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Edit — APX Dev</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ height: 100%; overflow: hidden; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d0d0d; color: #e8e8e8; display: flex; flex-direction: column; }}
  /* ── Header ── */
  header {{ padding: 0 16px; background: #111; border-bottom: 1px solid #2a2a2a;
            display: flex; align-items: center; gap: 10px; flex-shrink: 0; height: 44px; }}
  .badge {{ background: #1e3a5f; color: #60b0ff; font-size: 11px; font-weight: 600;
            padding: 2px 8px; border-radius: 4px; letter-spacing: .5px; text-transform: uppercase; }}
  h1 {{ font-size: 15px; font-weight: 600; color: #fff; }}
  nav {{ margin-left: auto; display: flex; gap: 4px; }}
  nav a {{ font-size: 12px; color: #888; text-decoration: none; padding: 3px 10px;
           border-radius: 5px; border: 1px solid transparent; }}
  nav a:hover {{ color: #ccc; border-color: #333; }}
  nav a.active {{ color: #60b0ff; background: #0d1f38; border-color: #1e3a5f; }}
  #apx-banner {{ background: #2a1a00; border-bottom: 1px solid #5a3a00; color: #ffb84d;
                 padding: 8px 16px; font-size: 13px; flex-shrink: 0; }}
  /* ── Split layout ── */
  #workspace {{ flex: 1; display: flex; overflow: hidden; }}
  #editor-wrap {{ flex: 1; overflow: hidden; display: flex; flex-direction: column;
                  border-right: 1px solid #1e1e1e; }}
  #editor-wrap .cm-editor {{ flex: 1; min-height: 0; }}
  #editor-wrap > div {{ flex: 1; min-height: 0; display: flex; flex-direction: column; }}
  #editor-wrap .cm-scroller {{ overflow: auto; flex: 1; }}
  /* ── Schema panel ── */
  #schema-panel {{ width: 300px; flex-shrink: 0; display: flex; flex-direction: column;
                   background: #0a0a0a; overflow: hidden; }}
  #schema-header {{ padding: 10px 14px; border-bottom: 1px solid #1e1e1e; flex-shrink: 0;
                    display: flex; align-items: center; justify-content: space-between; }}
  #schema-header span {{ font-size: 11px; font-weight: 600; color: #555;
                          text-transform: uppercase; letter-spacing: .6px; }}
  #schema-header .schema-hint {{ font-size: 10px; color: #333; font-style: italic; }}
  #schema-list {{ flex: 1; overflow-y: auto; padding: 8px; }}
  .tool-card {{ background: #111; border: 1px solid #1e1e1e; border-radius: 6px;
                padding: 10px 12px; margin-bottom: 8px; }}
  .tool-name {{ font-size: 12px; font-weight: 600; color: #e8e8e8; font-family: monospace;
                margin-bottom: 4px; }}
  .tool-desc {{ font-size: 11px; color: #666; margin-bottom: 6px; line-height: 1.4; }}
  .tool-params {{ display: flex; flex-direction: column; gap: 3px; }}
  .param-row {{ display: flex; gap: 6px; align-items: baseline; }}
  .param-name {{ font-size: 11px; font-family: monospace; color: #a5f3fc; }}
  .param-type {{ font-size: 10px; color: #555; }}
  .param-desc {{ font-size: 10px; color: #444; }}
  .no-params {{ font-size: 10px; color: #333; font-style: italic; }}
  .schema-error {{ font-size: 11px; color: #f87171; font-family: monospace; padding: 8px; }}
  /* ── Status bar ── */
  #status-bar {{ background: #111; border-top: 1px solid #2a2a2a; padding: 7px 14px;
                 display: flex; align-items: center; gap: 10px; flex-shrink: 0; }}
  #btn-save {{ background: #2563eb; color: #fff; border: none; border-radius: 6px;
               padding: 5px 14px; font-size: 13px; cursor: pointer; font-weight: 500; }}
  #btn-save:hover {{ background: #1d4ed8; }}
  #btn-new-tool {{ background: transparent; color: #60b0ff; border: 1px solid #1e3a5f;
                   border-radius: 6px; padding: 5px 14px; font-size: 13px; cursor: pointer; }}
  #btn-new-tool:hover {{ background: #0d1f38; }}
  #status-msg {{ font-size: 12px; font-family: monospace; }}
  #status-msg.ok {{ color: #4ade80; }}
  #status-msg.err {{ color: #f87171; }}
  kbd {{ background: #222; border: 1px solid #333; border-radius: 3px;
         padding: 1px 4px; font-size: 10px; color: #777; }}
  /* ── New Tool modal ── */
  #modal-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.7);
                    display: none; align-items: center; justify-content: center; z-index: 100; }}
  #modal-overlay.open {{ display: flex; }}
  #modal {{ background: #141414; border: 1px solid #2a2a2a; border-radius: 10px;
            width: 560px; max-height: 90vh; overflow-y: auto; display: flex;
            flex-direction: column; }}
  #modal-head {{ padding: 16px 20px; border-bottom: 1px solid #1e1e1e;
                 display: flex; align-items: center; justify-content: space-between; }}
  #modal-head h2 {{ font-size: 14px; font-weight: 600; }}
  #modal-close {{ background: none; border: none; color: #555; font-size: 18px;
                  cursor: pointer; line-height: 1; padding: 2px 6px; }}
  #modal-close:hover {{ color: #ccc; }}
  #modal-body {{ padding: 20px; display: flex; flex-direction: column; gap: 14px; }}
  .field {{ display: flex; flex-direction: column; gap: 5px; }}
  .field label {{ font-size: 11px; font-weight: 600; color: #888;
                  text-transform: uppercase; letter-spacing: .4px; }}
  .field input, .field textarea, .field select {{
    background: #0d0d0d; border: 1px solid #2a2a2a; color: #e8e8e8;
    border-radius: 6px; padding: 7px 10px; font-size: 13px; font-family: monospace;
    outline: none; resize: vertical; }}
  .field input:focus, .field textarea:focus, .field select:focus {{ border-color: #3a7bd5; }}
  .field select {{ cursor: pointer; }}
  #param-rows {{ display: flex; flex-direction: column; gap: 6px; }}
  .param-row-form {{ display: grid; grid-template-columns: 1fr 100px 1fr auto;
                     gap: 6px; align-items: center; }}
  .param-row-form input, .param-row-form select {{
    background: #0d0d0d; border: 1px solid #2a2a2a; color: #e8e8e8;
    border-radius: 5px; padding: 5px 8px; font-size: 12px; font-family: monospace; outline: none; }}
  .param-row-form input:focus, .param-row-form select:focus {{ border-color: #3a7bd5; }}
  .btn-rm-param {{ background: none; border: none; color: #555; cursor: pointer;
                   font-size: 16px; line-height: 1; padding: 2px 4px; }}
  .btn-rm-param:hover {{ color: #f87171; }}
  #btn-add-param {{ background: none; border: 1px dashed #2a2a2a; color: #555;
                    border-radius: 6px; padding: 6px; font-size: 12px;
                    cursor: pointer; text-align: center; margin-top: 2px; }}
  #btn-add-param:hover {{ border-color: #3a7bd5; color: #60b0ff; }}
  #modal-preview {{ background: #0d0d0d; border: 1px solid #1e1e1e; border-radius: 6px;
                    padding: 12px; font-size: 12px; font-family: monospace;
                    color: #a5f3fc; white-space: pre; overflow-x: auto; line-height: 1.5; }}
  #modal-foot {{ padding: 14px 20px; border-top: 1px solid #1e1e1e;
                 display: flex; justify-content: flex-end; gap: 8px; }}
  #btn-insert {{ background: #2563eb; color: #fff; border: none; border-radius: 6px;
                 padding: 7px 18px; font-size: 13px; cursor: pointer; font-weight: 500; }}
  #btn-insert:hover {{ background: #1d4ed8; }}
  #btn-cancel {{ background: transparent; color: #888; border: 1px solid #333;
                 border-radius: 6px; padding: 7px 14px; font-size: 13px; cursor: pointer; }}
  #btn-cancel:hover {{ color: #ccc; border-color: #555; }}
</style>
</head>
<body>
<header>
  <span class="badge">APX dev</span>
  <h1>Edit</h1>
  <nav>
    <a href="/_apx/agent">Chat</a>
    <a href="/_apx/edit" class="active">Edit</a>
    <a href="/_apx/setup">Setup</a>
  </nav>
  <button id="btn-deploy">Deploy ▶</button>
</header>
{not_found_banner}
<div id="workspace">
  <div id="editor-wrap"></div>
  <div id="schema-panel">
    <div id="schema-header">
      <span>Tool Schemas</span>
      <span class="schema-hint">what the model sees</span>
    </div>
    <div id="schema-list"><p class="no-params" style="padding:12px">Loading…</p></div>
  </div>
</div>
<div id="status-bar">
  <button id="btn-save">Save &nbsp;<kbd>⌘S</kbd></button>
  <button id="btn-new-tool">+ New Tool</button>
  <span id="status-msg"></span>
  <span style="margin-left:auto;font-size:11px;color:#333">agent_router.py</span>
</div>

<!-- New Tool modal -->
<div id="modal-overlay">
  <div id="modal">
    <div id="modal-head">
      <h2>New Tool</h2>
      <button id="modal-close">✕</button>
    </div>
    <div id="modal-body">
      <div class="field">
        <label>Function name</label>
        <input id="f-name" type="text" placeholder="my_tool" spellcheck="false">
      </div>
      <div class="field">
        <label>Description <span style="font-weight:400;text-transform:none;color:#444">(shown to the model)</span></label>
        <textarea id="f-desc" rows="2" placeholder="What this tool does for the agent"></textarea>
      </div>
      <div class="field">
        <label>Parameters</label>
        <div id="param-rows"></div>
        <button id="btn-add-param">+ Add parameter</button>
      </div>
      <div class="field">
        <label>Returns</label>
        <select id="f-return">
          <option value="str">str</option>
          <option value="list[str]">list[str]</option>
          <option value="dict[str, Any]">dict[str, Any]</option>
          <option value="int">int</option>
          <option value="float">float</option>
          <option value="bool">bool</option>
        </select>
      </div>
      <div class="field">
        <label>Preview</label>
        <pre id="modal-preview"></pre>
      </div>
    </div>
    <div id="modal-foot">
      <button id="btn-cancel">Cancel</button>
      <button id="btn-insert">Insert Tool</button>
    </div>
  </div>
</div>

<script type="module">
// Pin @codemirror/state@6.6.0 across all packages so every extension shares one
// module instance — this is the only package that matters for instanceof checks.
// keymap comes from @codemirror/view directly (meta-package drops re-exports when state is external).
import {{ EditorView, basicSetup }} from 'https://esm.sh/codemirror@6.0.1?deps=@codemirror/state@6.6.0';
import {{ keymap }} from 'https://esm.sh/@codemirror/view@6.41.0?deps=@codemirror/state@6.6.0';
import {{ python }} from 'https://esm.sh/@codemirror/lang-python@6.1.6?deps=@codemirror/state@6.6.0';
import {{ oneDark }} from 'https://esm.sh/@codemirror/theme-one-dark@6.1.2?deps=@codemirror/state@6.6.0';

// ── Editor ──────────────────────────────────────────────────────────────────
const INITIAL  = {content_js};
const WS_TYPE  = {ws_type_js};  // AppClient alias detected from source

const view = new EditorView({{
  doc: INITIAL,
  extensions: [
    basicSetup,
    python(),
    oneDark,
    keymap.of([{{ key: 'Mod-s', run: () => {{ save(); return true; }} }}]),
    EditorView.updateListener.of(v => {{ if (v.docChanged) schedulePreview(); }}),
    EditorView.theme({{ '&': {{ height: '100%' }}, '.cm-scroller': {{ overflow: 'auto' }} }}),
  ],
  parent: document.getElementById('editor-wrap'),
}});

// ── Save ────────────────────────────────────────────────────────────────────
async function save() {{
  const content = view.state.doc.toString();
  const msg = document.getElementById('status-msg');
  msg.textContent = 'Saving…'; msg.className = '';
  try {{
    const r = await fetch('/_apx/edit', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ content }}),
    }});
    const d = await r.json();
    if (d.ok) {{
      msg.textContent = '✓ Saved — reloading tools…'; msg.className = 'ok';
      setTimeout(() => {{ if (msg.className === 'ok') msg.textContent = ''; }}, 4000);
      refreshPreview(content);
    }} else {{
      msg.textContent = '✗ ' + d.error; msg.className = 'err';
    }}
  }} catch (e) {{ msg.textContent = '✗ ' + e.message; msg.className = 'err'; }}
}}
document.getElementById('btn-save').addEventListener('click', save);

// ── Schema preview ──────────────────────────────────────────────────────────
let previewTimer = null;
function schedulePreview() {{
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => refreshPreview(view.state.doc.toString()), 600);
}}

async function refreshPreview(source) {{
  try {{
    const r = await fetch('/_apx/edit/preview', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ source }}),
    }});
    const schemas = await r.json();
    renderSchemas(schemas);
  }} catch (e) {{ /* silent */ }}
}}

function renderSchemas(schemas) {{
  const el = document.getElementById('schema-list');
  if (!schemas.length) {{
    el.innerHTML = '<p class="no-params" style="padding:12px;color:#333">No tools found</p>';
    return;
  }}
  el.innerHTML = schemas.map(s => {{
    if (s._error) return `<p class="schema-error">${{s._error}}</p>`;
    const props = s.parameters?.properties ?? {{}};
    const paramHtml = Object.keys(props).length
      ? Object.entries(props).map(([k, v]) =>
          `<div class="param-row"><span class="param-name">${{k}}</span>`
          + `<span class="param-type">${{v.type}}</span>`
          + (v.description ? `<span class="param-desc">— ${{v.description}}</span>` : '')
          + `</div>`).join('')
      : '<span class="no-params">No parameters</span>';
    return `<div class="tool-card">
      <div class="tool-name">${{s.name}}</div>
      <div class="tool-desc">${{s.description || '<em style="color:#333">No description</em>'}}</div>
      <div class="tool-params">${{paramHtml}}</div>
    </div>`;
  }}).join('');
}}

// Initial preview
refreshPreview(INITIAL);

// ── New Tool modal ───────────────────────────────────────────────────────────
const overlay = document.getElementById('modal-overlay');
const TYPES = ['str','int','float','bool','list[str]','dict[str, Any]'];

function typeSelect(val='str') {{
  return `<select class="p-type">${{TYPES.map(t=>`<option${{t===val?' selected':''}}>${{t}}</option>`).join('')}}</select>`;
}}

function addParamRow(name='', type='str', desc='') {{
  const row = document.createElement('div');
  row.className = 'param-row-form';
  row.innerHTML = `<input class="p-name" placeholder="param_name" value="${{name}}" spellcheck="false">`
    + typeSelect(type)
    + `<input class="p-desc" placeholder="description" value="${{desc}}">`
    + `<button class="btn-rm-param" title="Remove">✕</button>`;
  row.querySelector('.btn-rm-param').onclick = () => {{ row.remove(); updatePreview(); }};
  row.querySelectorAll('input,select').forEach(el => el.addEventListener('input', updatePreview));
  document.getElementById('param-rows').appendChild(row);
  updatePreview();
}}

function collectParams() {{
  return [...document.querySelectorAll('.param-row-form')].map(r => ({{
    name: r.querySelector('.p-name').value.trim(),
    type: r.querySelector('.p-type').value,
    desc: r.querySelector('.p-desc').value.trim(),
  }})).filter(p => p.name);
}}

function buildFunctionCode() {{
  const name = (document.getElementById('f-name').value.trim() || 'my_tool').replace(/\\W/g,'_');
  const desc = document.getElementById('f-desc').value.trim() || 'Describe what this tool does.';
  const ret  = document.getElementById('f-return').value;
  const params = collectParams();

  const sigParams = params.map(p => `${{p.name}}: ${{p.type}}`).join(', ');
  const sep = sigParams ? ', ' : '';
  const sig = `def ${{name}}(${{sigParams}}${{sep}}ws: ${{WS_TYPE}}) -> ${{ret}}:`;

  const docLines = [desc];
  params.forEach(p => {{ if (p.desc) docLines.push(`    ${{p.name}}: ${{p.desc}}`); }});
  const docstring = docLines.length > 1
    ? `    {chr(34)*3}${{docLines[0]}}\\n${{docLines.slice(1).join('\\n')}}\\n    {chr(34)*3}`
    : `    {chr(34)*3}${{docLines[0]}}{chr(34)*3}`;

  return `${{sig}}\\n${{docstring}}\\n    # TODO: implement your tool\\n    pass`;
}}

function updatePreview() {{
  document.getElementById('modal-preview').textContent = buildFunctionCode();
}}

document.getElementById('btn-new-tool').addEventListener('click', () => {{
  document.getElementById('f-name').value = '';
  document.getElementById('f-desc').value = '';
  document.getElementById('f-return').value = 'str';
  document.getElementById('param-rows').innerHTML = '';
  updatePreview();
  overlay.classList.add('open');
  document.getElementById('f-name').focus();
}});
document.getElementById('modal-close').onclick =
document.getElementById('btn-cancel').onclick = () => overlay.classList.remove('open');
overlay.addEventListener('click', e => {{ if (e.target === overlay) overlay.classList.remove('open'); }});
document.getElementById('btn-add-param').onclick = () => addParamRow();

document.getElementById('f-name').addEventListener('input', updatePreview);
document.getElementById('f-desc').addEventListener('input', updatePreview);
document.getElementById('f-return').addEventListener('input', updatePreview);

document.getElementById('btn-insert').addEventListener('click', () => {{
  const code = buildFunctionCode();
  const fnName = (document.getElementById('f-name').value.trim() || 'my_tool').replace(/\\W/g,'_');
  const stub = '\\n\\n' + code;

  const doc = view.state.doc.toString();
  // 1. Insert function before agent = Agent(...)
  const agentMarker = '\\nagent = Agent(';
  const insertAt = doc.lastIndexOf(agentMarker) !== -1 ? doc.lastIndexOf(agentMarker) : doc.length;

  // 2. Add to agent = Agent(tools=[...]) list
  const agentLine = doc.lastIndexOf('agent = Agent(tools=[');
  let changes = [{{ from: insertAt, to: insertAt, insert: stub }}];
  if (agentLine !== -1) {{
    const closeBracket = doc.indexOf('])', agentLine);
    if (closeBracket !== -1) {{
      const inside = doc.slice(agentLine, closeBracket);
      const sep = inside.trimEnd().endsWith('[') ? '' : ', ';
      changes.push({{ from: closeBracket, to: closeBracket, insert: sep + fnName }});
    }}
  }}

  view.dispatch(view.state.update({{
    changes,
    selection: {{ anchor: insertAt + stub.indexOf(fnName) }},
  }}));
  overlay.classList.remove('open');
  view.focus();
}});
</script>
{_deploy_overlay_html()}
</body>
</html>"""


