"""Dev UI — /_apx/edit in-browser agent_router.py editor and source manipulation utilities."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ._models import AgentContext
from ._ui_nav import (
    _apx_nav_css,
    _apx_nav_html,
    _apx_nav_links,
    _deploy_overlay_html,
    _topology_minimap_html,
)

# ---------------------------------------------------------------------------
# /_apx/edit — in-browser agent_router.py editor
# ---------------------------------------------------------------------------


def _find_agent_router_path() -> "Path | None":
    """Return the path to the agent definition file.

    Looks for two layouts in priority order:

    1. **ADK flat** (current convention) — a top-level ``agent`` module that
       lives next to ``pyproject.toml`` (e.g. ``customer_triage/agent.py``).
       The module is imported as ``agent`` because the example dir is on
       ``sys.path`` via ``pythonpath = ["."]`` in pyproject.
    2. **Legacy nested** (pre-flatten, kept for backwards-compat) — a
       ``<pkg>.backend.agent_router`` module under ``src/<pkg>/backend/``.

    Returns the resolved ``Path`` of whichever is loaded in this process,
    or ``None`` if neither is present.
    """
    import sys
    from pathlib import Path

    # ADK flat: a top-level "agent" module. Filter to a plausible apx-agent
    # entrypoint by checking the file is named ``agent.py`` — that excludes
    # any unrelated module also called "agent" that might be in sys.modules.
    flat = sys.modules.get("agent")
    if flat is not None:
        origin = getattr(flat, "__file__", None)
        if origin and Path(origin).name == "agent.py":
            return Path(origin)

    # Legacy nested layout — kept for old examples that still use
    # src/<pkg>/backend/agent_router.py.
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



# Leaf-agent constructors the editor can wire tools into, mapped to the kwarg
# each uses for its tool list. DataAgent (an LlmAgent subclass) builds its own
# ``tools=`` list internally and takes additional tools via ``extra_tools=``,
# so generated tools must attach there, not to a (nonexistent) ``tools=``.
_LEAF_AGENT_TOOL_KWARG = {
    "Agent": "tools",
    "LlmAgent": "tools",
    "DataAgent": "extra_tools",
}


def _call_func_name(call: Any) -> str:
    """Constructor name of an ``ast.Call`` (e.g. ``Agent``, ``DataAgent``)."""
    import ast as _ast

    fn = call.func
    if isinstance(fn, _ast.Name):
        return fn.id
    if isinstance(fn, _ast.Attribute):
        return fn.attr
    return ""


def _tool_kwarg_for(call: Any) -> str:
    """Which kwarg holds the wireable tool list for this agent call."""
    return _LEAF_AGENT_TOOL_KWARG.get(_call_func_name(call), "tools")


def _agent_tool_names(source: str, target: str) -> list[str] | None:
    """Return the tool names in ``{target}``'s tool list (``tools=`` or, for
    DataAgent, ``extra_tools=``), or None if there is no recognized agent call
    (e.g. a composition wrapper)."""
    import ast as _ast

    call = _find_root_agent_call(source, target)
    if call is None:
        return None
    kwarg = _tool_kwarg_for(call)
    kw = next((k for k in call.keywords if k.arg == kwarg), None)
    if kw is None or not isinstance(kw.value, _ast.List):
        return []
    return [e.id for e in kw.value.elts if isinstance(e, _ast.Name)]


def _splice_tool(source: str, fn_code: str, fn_name: str, *, target: str = "agent") -> str:
    """Insert ``fn_code`` before the root agent assignment and register ``fn_name``
    in ``target``'s ``tools=`` list.

    ``target`` defaults to the root ``agent``. When the root is a composition
    wrapper (no ``Agent(...)`` of its own), pass the leaf agent's name to attach
    the tool there. Wiring is AST-based — order- and multi-line-agnostic. If the
    target has no ``Agent(...)`` call, the function is still defined but left
    unwired (callers can detect this and prompt for a target).
    """
    import ast as _ast
    import re as _re

    # Insert the function definition immediately before the TARGET agent's
    # assignment, so a leaf that references it is defined *after* the function
    # (inserting before the root would leave a leaf referencing an undefined name).
    insert_at: int | None = None
    try:
        for s in _ast.parse(source).body:
            if (isinstance(s, _ast.Assign) and s.targets
                    and isinstance(s.targets[0], _ast.Name) and s.targets[0].id == target):
                insert_at = _abs_offset(source, s.lineno, 0)
                break
    except SyntaxError:
        pass
    if insert_at is None:  # fall back to before the last `agent = <wrapper>(`
        for m in _re.finditer(r'\nagent\s*=\s*\w+\s*\(', source):
            insert_at = m.start() + 1
    if insert_at is None:
        result = source.rstrip("\n") + "\n\n" + fn_code + "\n"
    else:
        result = source[:insert_at] + fn_code + "\n\n" + source[insert_at:]

    try:
        existing = _agent_tool_names(result, target)
        if existing is not None and fn_name not in existing:
            result = _set_agent_tools(result, existing + [fn_name], target=target)
    except Exception:  # noqa: BLE001 — never block the function insert on a wiring hiccup
        pass

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
    """Remove a tool function (incl. its decorators) and drop it from the root
    agent's ``tools=`` list.

    AST-based: removes the whole ``FunctionDef`` — ``@tool`` decorator and all,
    no orphaned decorator left behind — and drops only the ``tools=`` entry,
    not other textual occurrences of the name (a string, comment, or another
    function's body). Returns ``source`` unchanged if the function isn't found.
    """
    import ast as _ast
    import re as _re

    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return source

    target = next(
        (
            n for n in tree.body
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name == fn_name
        ),
        None,
    )
    if target is None:
        return source

    # Span includes decorators (their lineno sits above the `def` line).
    start_line = min([d.lineno for d in target.decorator_list], default=target.lineno)
    end_line = target.end_lineno or target.lineno

    lines = source.splitlines(keepends=True)
    del lines[start_line - 1:end_line]
    result = _re.sub(r"\n{3,}", "\n\n", "".join(lines))

    # Drop the name from EVERY agent's tools= list (surgical, AST-based) — so it
    # is unwired whether it lived on the root or a leaf of a composition.
    try:
        for node in _parse_agent_nodes(result):
            names = node.get("tools") or []
            if fn_name in names:
                result = _set_agent_tools(
                    result, [n for n in names if n != fn_name], target=node["name"]
                )
    except Exception:  # noqa: BLE001
        pass

    return result


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
        if func_name not in _LEAF_AGENT_TOOL_KWARG:
            return None
        tool_kwarg = _LEAF_AGENT_TOOL_KWARG[func_name]
        tools: list[str] = []
        instructions: str = ""
        for kw in call_node.keywords:
            if kw.arg == tool_kwarg and isinstance(kw.value, _ast.List):
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
            inner_found = False
            for arg in val.args:
                inner = _extract_agent_call(arg)
                if inner is not None:
                    nodes.append({"name": var_name, "wrapper": wrapper_name, **inner})
                    inner_found = True
                    break
            if inner_found:
                continue
            # Composition: agent = SequentialAgent(agents=[a, b]) — leaves are
            # referenced by name. Record the wrapper + member names so the
            # composer round-trips the pattern and its members.
            if wrapper_name in _COMPOSITION_WRAPPERS:
                nodes.append({
                    "name": var_name, "wrapper": wrapper_name,
                    "tools": [], "instructions": "",
                    "members": _extract_member_names(val),
                })

    return nodes


def _extract_member_names(call: Any) -> list[str]:
    """Extract leaf-agent names referenced by a workflow wrapper's ``agents=``.

    Handles list-of-Names (Sequential/Parallel), list-of-tuples (Router:
    ``(key, desc, agent)``), and dict-of-Names (Handoff: ``{key: agent}``).
    """
    import ast as _ast

    kw = next((k for k in getattr(call, "keywords", []) if k.arg == "agents"), None)
    if kw is None:
        return []
    names: list[str] = []
    if isinstance(kw.value, _ast.List):
        for elt in kw.value.elts:
            if isinstance(elt, _ast.Name):
                names.append(elt.id)
            elif isinstance(elt, _ast.Tuple):  # Router (key, desc, agent)
                agent_elt = next((e for e in reversed(elt.elts) if isinstance(e, _ast.Name)), None)
                if agent_elt is not None:
                    names.append(agent_elt.id)
    elif isinstance(kw.value, _ast.Dict):  # Handoff {key: agent}
        names = [v.id for v in kw.value.values if isinstance(v, _ast.Name)]
    return names


def _py_str_literal(s: str) -> str:
    """Render a Python string literal — triple-quoted for readable multi-line
    content when safe, else a ``repr`` that escapes everything correctly.
    """
    if "\n" in s and '"""' not in s and "\\" not in s and not s.endswith('"'):
        return f'"""{s}"""'
    return repr(s)


def _abs_offset(source: str, lineno: int, col: int) -> int:
    """Convert a 1-based (lineno, col) AST position to an absolute char index."""
    lines = source.splitlines(keepends=True)
    return sum(len(line) for line in lines[: lineno - 1]) + col


def _find_root_agent_call(source: str, target: str) -> Any:
    """Return the ``Agent()``/``LlmAgent()`` Call node assigned to ``target``.

    Handles both direct (``agent = Agent(...)``) and wrapped
    (``agent = LoopAgent(Agent(...))``) forms. Returns ``None`` if not found.
    """
    import ast as _ast

    def _find(value: Any) -> Any:
        if isinstance(value, _ast.Call):
            fn = value.func
            fname = (
                fn.id if isinstance(fn, _ast.Name)
                else fn.attr if isinstance(fn, _ast.Attribute)
                else ""
            )
            if fname in _LEAF_AGENT_TOOL_KWARG:
                return value
            for arg in value.args:  # wrapped: Wrapper(Agent(...), ...)
                inner = _find(arg)
                if inner is not None:
                    return inner
        return None

    for stmt in _ast.parse(source).body:
        if (
            isinstance(stmt, _ast.Assign)
            and stmt.targets
            and isinstance(stmt.targets[0], _ast.Name)
            and stmt.targets[0].id == target
        ):
            call = _find(stmt.value)
            if call is not None:
                return call
    return None


def _set_agent_kwarg(source: str, *, arg: str, literal: str, target: str) -> str:
    """Surgically replace (or insert) the ``arg=`` keyword of ``target``'s Agent
    call with the already-rendered ``literal`` source text, preserving every
    other argument. Raises ValueError if the agent call isn't found.
    """
    call = _find_root_agent_call(source, target)
    if call is None:
        raise ValueError(f"Could not find `{target} = Agent(...)` in the agent file")

    kw = next((k for k in call.keywords if k.arg == arg), None)
    if kw is not None:
        v = kw.value
        start = _abs_offset(source, v.lineno, v.col_offset)
        end = _abs_offset(source, v.end_lineno, v.end_col_offset)  # type: ignore[arg-type]
        return source[:start] + literal + source[end:]

    # Keyword absent — append before the call's closing ')'. Appending (rather
    # than inserting after '(') is required when the call has positional args,
    # e.g. DataAgent("samples", "nyctaxi") — a leading kwarg would be a
    # "positional argument follows keyword argument" SyntaxError.
    call_end = _abs_offset(source, call.end_lineno, call.end_col_offset)  # type: ignore[arg-type]
    close_paren = source.rfind(")", 0, call_end)
    before = source[:close_paren].rstrip()
    needs_comma = bool(call.args or call.keywords) and not before.endswith(("(", ","))
    piece = (", " if needs_comma else "") + f"{arg}={literal}"
    return source[:close_paren] + piece + source[close_paren:]


def _set_agent_instructions(source: str, instructions: str, *, target: str = "agent") -> str:
    """Set the ``instructions=`` argument of the root agent's call in ``source``.

    Surgically replaces only the ``instructions=`` keyword value of the
    ``{target} = Agent(...)`` / ``LlmAgent(...)`` call (direct or wrapped),
    preserving every other argument; inserts if absent. So Setup, the editor,
    and the running agent share one source of truth (agent.py).
    """
    return _set_agent_kwarg(source, arg="instructions", literal=_py_str_literal(instructions), target=target)


def _set_agent_tools(source: str, tools: list[str], *, target: str = "agent") -> str:
    """Set the ``tools=[...]`` argument of the root agent's call in ``source``.

    ``tools`` are tool-function identifier names (e.g. ``["echo", "lookup"]``);
    they are written as a bare name list (``tools=[echo, lookup]``), so the
    functions must be defined/imported in the agent file. Surgically replaces
    only the ``tools=`` value, preserving every other argument; inserts if absent.
    """
    literal = "[" + ", ".join(tools) + "]"
    call = _find_root_agent_call(source, target)
    kwarg = _tool_kwarg_for(call) if call is not None else "tools"
    return _set_agent_kwarg(source, arg=kwarg, literal=literal, target=target)


def _set_agent_wrapper(source: str, wrapper: str, *, target: str = "agent") -> str:
    """Change the single-agent wrapper of ``{target}`` while preserving the inner
    ``Agent(...)`` call verbatim — name=, sub_agents=, temperature=, everything.

    Supports the auto-applicable forms:
      * ``"Agent"`` / ``"LlmAgent"`` → unwrap to the bare ``Agent(...)`` call.
      * ``"LoopAgent"`` → wrap the inner call as ``LoopAgent(Agent(...))``.

    Reuses the inner call's exact source text rather than regenerating it, so no
    arguments are dropped. Raises ValueError if the target agent isn't found or
    ``wrapper`` isn't one of the supported single-agent forms.
    """
    import ast as _ast

    if wrapper not in ("Agent", "LlmAgent", "LoopAgent"):
        raise ValueError(f"Unsupported single-agent wrapper: {wrapper!r}")

    tree = _ast.parse(source)
    assign = next(
        (
            s for s in tree.body
            if isinstance(s, _ast.Assign)
            and s.targets and isinstance(s.targets[0], _ast.Name)
            and s.targets[0].id == target
        ),
        None,
    )
    if assign is None:
        raise ValueError(f"Could not find `{target} = ...` assignment")

    inner = _find_root_agent_call(source, target)
    if inner is None:
        raise ValueError(f"`{target}` is not an Agent(...) call")

    inner_src = source[
        _abs_offset(source, inner.lineno, inner.col_offset):
        _abs_offset(source, inner.end_lineno, inner.end_col_offset)  # type: ignore[arg-type]
    ]
    new_rhs = inner_src if wrapper in ("Agent", "LlmAgent") else f"LoopAgent({inner_src})"

    val = assign.value
    vstart = _abs_offset(source, val.lineno, val.col_offset)
    vend = _abs_offset(source, val.end_lineno, val.end_col_offset)  # type: ignore[arg-type]
    return source[:vstart] + new_rhs + source[vend:]


# ---------------------------------------------------------------------------
# Multi-agent composition — wire defined leaf agents into a workflow root
# ---------------------------------------------------------------------------

_COMPOSITION_WRAPPERS = {"SequentialAgent", "ParallelAgent", "RouterAgent", "HandoffAgent"}


def _ensure_apx_import(source: str, *names: str) -> str:
    """Ensure each of ``names`` is imported from ``apx_agent``.

    Appends missing names to an existing ``from apx_agent import ...`` statement
    (collapsing it to one line), or adds a new import after ``from __future__``
    if none exists.
    """
    import ast as _ast

    for node in _ast.parse(source).body:
        if isinstance(node, _ast.ImportFrom) and node.module == "apx_agent":
            ordered = [a.name for a in node.names]
            missing = [n for n in names if n not in ordered]
            if not missing:
                return source
            ordered += missing
            start = _abs_offset(source, node.lineno, node.col_offset)
            end = _abs_offset(source, node.end_lineno, node.end_col_offset)  # type: ignore[arg-type]
            return source[:start] + "from apx_agent import " + ", ".join(ordered) + source[end:]

    new_line = "from apx_agent import " + ", ".join(names) + "\n"
    lines = source.splitlines(keepends=True)
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.startswith("from __future__"):
            insert_idx = i + 1
            break
    lines.insert(insert_idx, new_line)
    return "".join(lines)


def _render_leaf_agent(name: str, tools: list[str], instructions: str) -> str:
    """Render a leaf-agent assignment: ``name = Agent(tools=[...], instructions=...)``."""
    return (
        f"{name} = Agent(tools=[{', '.join(tools)}], "
        f"instructions={_py_str_literal(instructions)})"
    )


def _compose_root_expr(pattern: str, leaves: list[dict[str, Any]], *, start: str | None = None) -> str:
    """Build the workflow-wrapper expression that composes the leaf agents.

    ``leaves`` is an ordered list of ``{name, route_key?, route_description?}``.
    Returns e.g. ``SequentialAgent(agents=[a, b])`` or
    ``RouterAgent(agents=[("k", "desc", a), ...])`` /
    ``HandoffAgent(agents={"k": a, ...}, start="k")``.
    """
    names = [leaf["name"] for leaf in leaves]
    if pattern in ("SequentialAgent", "ParallelAgent"):
        return f"{pattern}(agents=[{', '.join(names)}])"
    if pattern == "RouterAgent":
        tuples = ", ".join(
            f"({_py_str_literal(leaf.get('route_key') or leaf['name'])}, "
            f"{_py_str_literal(leaf.get('route_description') or '')}, {leaf['name']})"
            for leaf in leaves
        )
        return f"RouterAgent(agents=[{tuples}])"
    if pattern == "HandoffAgent":
        keys = [leaf.get("route_key") or leaf["name"] for leaf in leaves]
        entries = ", ".join(
            f"{_py_str_literal(k)}: {leaf['name']}" for k, leaf in zip(keys, leaves)
        )
        start_key = start or (keys[0] if keys else "")
        return f"HandoffAgent(agents={{{entries}}}, start={_py_str_literal(start_key)})"
    raise ValueError(f"Unsupported composition pattern: {pattern!r}")


def _compose_agents(
    source: str,
    pattern: str,
    leaves: list[dict[str, Any]],
    *,
    start: str | None = None,
) -> str:
    """Compose ``leaves`` into the root ``agent`` via the chosen workflow pattern.

    Each leaf (``{name, tools, instructions, route_key?, route_description?}``)
    is upserted as ``name = Agent(tools=[...], instructions=...)`` — existing
    ones patched surgically, new ones inserted before the root. The root
    ``agent =`` is then rewritten to the workflow wrapper referencing them, and
    the wrapper class is added to the apx_agent import.

    Raises ValueError on an unknown pattern or fewer than two leaves.
    """
    import ast as _ast

    if pattern not in _COMPOSITION_WRAPPERS:
        raise ValueError(f"Unsupported composition pattern: {pattern!r}")
    if len(leaves) < 2:
        raise ValueError("A composition needs at least two agents")

    existing = {n["name"] for n in _parse_agent_nodes(source)}
    result = source

    # 1. Upsert each leaf agent (skip the reserved root name "agent").
    for leaf in leaves:
        name = leaf["name"]
        if name == "agent":
            raise ValueError("Leaf agents cannot be named 'agent' (that is the root wrapper)")
        tools = leaf.get("tools") or []
        instr = leaf.get("instructions") or ""
        if name in existing:
            result = _set_agent_tools(result, tools, target=name)
            result = _set_agent_instructions(result, instr, target=name)
        else:
            # Insert the new leaf assignment just before the root `agent =`.
            tree = _ast.parse(result)
            root = next(
                (
                    s for s in tree.body
                    if isinstance(s, _ast.Assign) and s.targets
                    and isinstance(s.targets[0], _ast.Name) and s.targets[0].id == "agent"
                ),
                None,
            )
            leaf_line = _render_leaf_agent(name, tools, instr) + "\n"
            if root is None:
                result = result.rstrip("\n") + "\n" + leaf_line
            else:
                at = _abs_offset(result, root.lineno, 0)  # type: ignore[arg-type]
                result = result[:at] + leaf_line + result[at:]

    # 2. Rewrite the root `agent =` value to the workflow wrapper.
    root_expr = _compose_root_expr(pattern, leaves, start=start)
    tree = _ast.parse(result)
    root_assign = next(
        (
            s for s in tree.body
            if isinstance(s, _ast.Assign) and s.targets
            and isinstance(s.targets[0], _ast.Name) and s.targets[0].id == "agent"
        ),
        None,
    )
    if root_assign is None:
        result = result.rstrip("\n") + "\nagent = " + root_expr + "\n"
    else:
        val = root_assign.value
        vstart = _abs_offset(result, val.lineno, val.col_offset)  # type: ignore[arg-type]
        vend = _abs_offset(result, val.end_lineno, val.end_col_offset)  # type: ignore[arg-type]
        result = result[:vstart] + root_expr + result[vend:]

    # 3. Ensure the wrapper class (and Agent) are imported.
    result = _ensure_apx_import(result, "Agent", pattern)
    return result


def _render_edit_ui(content: str, not_found: bool = False) -> str:
    """Return a split-panel authoring page: CodeMirror left, schema preview right.

    Left panel — editable agent source (``agent.py`` in the ADK flat layout
    or ``agent_router.py`` in the legacy nested layout) with Python syntax
    highlighting.
    Right panel — live tool schemas (what the model sees) updated on debounce.
    New Tool modal — structured form that generates correct function boilerplate.

    Cmd/Ctrl+S saves the file; restart `apx run` to load the new agent code.
    """
    import json as _json
    import re as _re

    content_js = _json.dumps(content)
    # Surface the actual filename in the status bar so users know which file
    # the editor is bound to.
    _ar = _find_agent_router_path()
    file_label = _ar.name if _ar else "agent source"
    # Detect the AppClient alias (e.g. "Client = Dependencies.Client" → "Client")
    _alias_m = _re.search(r"^(\w+)\s*=\s*Dependencies\.Client", content, _re.MULTILINE)
    ws_type = _alias_m.group(1) if _alias_m else "AppClient"
    ws_type_js = _json.dumps(ws_type)
    not_found_banner = (
        '<div id="apx-banner"><strong>⚠ agent source not found</strong> — '
        "looked for a top-level <code>agent.py</code> (ADK flat layout) and a "
        "<code>&lt;pkg&gt;.backend.agent_router</code> module (legacy nested layout); "
        "neither is loaded in this process.</div>"
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
  #btn-from-data {{ background: transparent; color: #9d7bff; border: 1px solid #3a2d5f;
    border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; margin-left: 8px; }}
  #btn-from-data:hover {{ background: #1a1230; }}
  /* "From data" modal — hosts the Setup flow (data source + schema-grounded gen) in an iframe */
  #data-modal-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.7);
    display: none; align-items: center; justify-content: center; z-index: 200; }}
  #data-modal-overlay.open {{ display: flex; }}
  #data-modal {{ width: 88vw; max-width: 940px; height: 86vh; background: #0d0d0d;
    border: 1px solid #2a2a2a; border-radius: 12px; display: flex; flex-direction: column;
    overflow: hidden; box-shadow: 0 20px 60px rgba(0,0,0,.6); }}
  #data-modal-head {{ display: flex; align-items: center; gap: 10px; padding: 12px 16px;
    border-bottom: 1px solid #1e1e1e; }}
  #data-modal-head h2 {{ font-size: 14px; color: #fff; flex: 1; margin: 0; }}
  #data-modal-close {{ background: none; border: none; color: #555; font-size: 18px;
    cursor: pointer; line-height: 1; }}
  #data-modal-close:hover {{ color: #ccc; }}
  #data-modal iframe {{ flex: 1; width: 100%; border: none; background: #0d0d0d; }}
  #apx-toast {{ position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%);
    background: #0d2a0d; color: #4ade80; border: 1px solid #1a4a1a; border-radius: 8px;
    padding: 10px 18px; font-size: 13px; z-index: 300; opacity: 0; transition: opacity .2s;
    pointer-events: none; }}
  #apx-toast.show {{ opacity: 1; }}
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
  /* Natural-language generate section */
  #f-gen {{ background: #0c1a2e; border: 1px solid #1e3a5f; border-radius: 8px;
            padding: 12px; display: flex; flex-direction: column; gap: 8px; }}
  #f-gen > label {{ font-size: 11px; font-weight: 600; color: #60b0ff;
                    text-transform: uppercase; letter-spacing: .4px; }}
  #f-gen-row {{ display: flex; gap: 8px; align-items: stretch; }}
  #f-prompt {{ flex: 1; }}
  #btn-generate {{ background: #1e3a5f; color: #cfe6ff; border: 1px solid #2d568a;
                   border-radius: 6px; padding: 7px 14px; font-size: 13px; cursor: pointer;
                   font-weight: 500; white-space: nowrap; }}
  #btn-generate:hover {{ background: #285080; }}
  #btn-generate:disabled {{ opacity: .5; cursor: default; }}
  #gen-msg {{ font-size: 11px; color: #5a7fae; min-height: 14px; }}
  #btn-cancel {{ background: transparent; color: #888; border: 1px solid #333;
                 border-radius: 6px; padding: 7px 14px; font-size: 13px; cursor: pointer; }}
  #btn-cancel:hover {{ color: #ccc; border-color: #555; }}
</style>
</head>
<body>
<header>
  <span class="badge">APX dev</span>
  <h1>Edit</h1>
  <nav>{_apx_nav_links("edit")}</nav>
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
  <button id="btn-from-data">✨ From data</button>
  <span id="status-msg"></span>
  <span style="margin-left:auto;font-size:11px;color:#333">{file_label}</span>
</div>

<!-- Generate-from-data modal: hosts the Setup flow (data source + schema-grounded tool gen) -->
<div id="data-modal-overlay">
  <div id="data-modal">
    <div id="data-modal-head">
      <h2>✨ Generate a tool from your data</h2>
      <button id="data-modal-close" title="Close">✕</button>
    </div>
    <iframe id="data-modal-frame" title="Generate from data"></iframe>
  </div>
</div>
<div id="apx-toast"></div>

<!-- New Tool modal -->
<div id="modal-overlay">
  <div id="modal">
    <div id="modal-head">
      <h2>New Tool</h2>
      <button id="modal-close">✕</button>
    </div>
    <div id="modal-body">
      <div id="f-gen">
        <label>Describe it <span style="font-weight:400;text-transform:none;color:#5a7fae">— generate the tool from natural language</span></label>
        <div id="f-gen-row">
          <textarea id="f-prompt" rows="2" placeholder="e.g. get a customer's total spend by month for a given year"></textarea>
          <button id="btn-generate" type="button">✨ Generate</button>
        </div>
        <span id="gen-msg"></span>
      </div>
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
        <label>Attach to agent</label>
        <select id="f-agent"><option value="agent">agent</option></select>
      </div>
      <div class="field">
        <label>Body <span style="font-weight:400;text-transform:none;color:#444">(generated — edit before inserting; leave blank for a stub)</span></label>
        <textarea id="f-body" rows="6" placeholder="# generated implementation appears here after Generate" spellcheck="false"></textarea>
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

  const body = document.getElementById('f-body').value.replace(/\\s+$/,'');
  const bodyBlock = body ? body : '    # TODO: implement your tool\\n    pass';
  return `${{sig}}\\n${{docstring}}\\n${{bodyBlock}}`;
}}

function updatePreview() {{
  document.getElementById('modal-preview').textContent = buildFunctionCode();
}}

async function populateAgentPicker() {{
  const sel = document.getElementById('f-agent');
  try {{
    const r = await fetch('/_apx/setup/agents');
    const nodes = r.ok ? await r.json() : [];
    const list = Array.isArray(nodes) ? nodes : [];
    // Offer the leaf agents first (where tools usually belong), then the root.
    const names = [...list.filter(n => n.name !== 'agent' && !n.wrapper).map(n => n.name),
                   ...list.filter(n => n.name === 'agent').map(n => n.name)];
    const opts = names.length ? names : ['agent'];
    sel.innerHTML = opts.map(n => `<option value="${{n}}">${{n}}</option>`).join('');
  }} catch (e) {{ sel.innerHTML = '<option value="agent">agent</option>'; }}
}}

document.getElementById('btn-new-tool').addEventListener('click', () => {{
  document.getElementById('f-prompt').value = '';
  document.getElementById('gen-msg').textContent = '';
  document.getElementById('f-name').value = '';
  document.getElementById('f-desc').value = '';
  document.getElementById('f-return').value = 'str';
  document.getElementById('f-body').value = '';
  document.getElementById('param-rows').innerHTML = '';
  populateAgentPicker();
  updatePreview();
  overlay.classList.add('open');
  document.getElementById('f-prompt').focus();
}});

// Natural-language generation: describe the tool, the model fills the fields
// below (name/description/params/body) for review before inserting.
document.getElementById('btn-generate').addEventListener('click', async () => {{
  const prompt = document.getElementById('f-prompt').value.trim();
  const gmsg = document.getElementById('gen-msg');
  const gbtn = document.getElementById('btn-generate');
  if (!prompt) {{ gmsg.textContent = 'Type a description first.'; return; }}
  gbtn.disabled = true; gbtn.textContent = 'Generating…'; gmsg.textContent = '';
  try {{
    // Persist the buffer first so the generator grounds on the latest source
    // (it mines table/column names from agent.py).
    await fetch('/_apx/edit', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ content: view.state.doc.toString() }}),
    }});
    const r = await fetch('/_apx/tools/suggest', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ prompt }}),
    }});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Generation failed');
    const s = d.spec || {{}};
    document.getElementById('f-name').value = s.name || '';
    document.getElementById('f-desc').value = s.description || '';
    document.getElementById('param-rows').innerHTML = '';
    (s.params || []).forEach(p => addParamRow(p.name || '', p.type || 'str', p.desc || ''));
    if (s.returns) document.getElementById('f-return').value = s.returns;
    document.getElementById('f-body').value = s.body || '';
    updatePreview();
    gmsg.textContent = '✓ Generated — review the fields below, then Insert.';
  }} catch (e) {{
    gmsg.textContent = '✗ ' + e.message;
  }} finally {{
    gbtn.disabled = false; gbtn.textContent = '✨ Generate';
  }}
}});
document.getElementById('modal-close').onclick =
document.getElementById('btn-cancel').onclick = () => overlay.classList.remove('open');
overlay.addEventListener('click', e => {{ if (e.target === overlay) overlay.classList.remove('open'); }});
document.getElementById('btn-add-param').onclick = () => addParamRow();

document.getElementById('f-name').addEventListener('input', updatePreview);
document.getElementById('f-desc').addEventListener('input', updatePreview);
document.getElementById('f-return').addEventListener('input', updatePreview);
document.getElementById('f-body').addEventListener('input', updatePreview);

document.getElementById('btn-insert').addEventListener('click', async () => {{
  const btn = document.getElementById('btn-insert');
  const name = (document.getElementById('f-name').value.trim() || 'my_tool').replace(/\\W/g,'_');
  const spec = {{
    name,
    description: document.getElementById('f-desc').value.trim() || 'Describe what this tool does.',
    params: collectParams(),
    returns: document.getElementById('f-return').value,
    body: document.getElementById('f-body').value.replace(/\\s+$/,'') || null,
    agent: document.getElementById('f-agent').value || 'agent',
  }};
  btn.disabled = true; btn.textContent = 'Inserting…';
  try {{
    // 1. Persist the current buffer so the backend splices into the latest
    //    source (it reads the file, not this editor buffer).
    const sv = await fetch('/_apx/edit', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ content: view.state.doc.toString() }}),
    }});
    const svd = await sv.json();
    if (!svd.ok) throw new Error(svd.error || 'Could not save current edits');
    // 2. Create + wire the tool via the backend (correct AST splice + target).
    const r = await fetch('/_apx/tools/new', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(spec),
    }});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Could not create tool');
    if (d.wired === false && d.note) alert(d.note);
    // 3. Reload so the editor shows the updated agent.py.
    window.location.reload();
  }} catch (e) {{
    btn.disabled = false; btn.textContent = 'Insert Tool';
    alert(e.message);
  }}
}});

// "From data" modal — host the Setup flow in an iframe; toast on creation and
// reload on close so the editor + schema panel pick up the new agent_router.py.
(function() {{
  const dOverlay = document.getElementById('data-modal-overlay');
  const dFrame   = document.getElementById('data-modal-frame');
  const toast    = document.getElementById('apx-toast');
  let createdInModal = false;
  function openData() {{
    if (!dFrame.getAttribute('src')) dFrame.setAttribute('src', '/_apx/setup?embed=1');
    dOverlay.classList.add('open');
  }}
  function closeData() {{
    dOverlay.classList.remove('open');
    if (createdInModal) window.location.reload();
  }}
  document.getElementById('btn-from-data').addEventListener('click', openData);
  document.getElementById('data-modal-close').addEventListener('click', closeData);
  dOverlay.addEventListener('click', e => {{ if (e.target === dOverlay) closeData(); }});
  function showToast(msg) {{
    toast.textContent = msg; toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2800);
  }}
  window.addEventListener('message', e => {{
    const d = e.data || {{}};
    if (d && d.type === 'apx:tool-created') {{
      createdInModal = true;
      const what = d.name ? `✓ Created ${{d.name}}` : `✓ Created ${{d.count || ''}} tool(s)`;
      showToast(`${{what}} — close to see it in your agent`);
    }}
  }});
}})();
</script>
{_deploy_overlay_html()}
{_topology_minimap_html()}
</body>
</html>"""


