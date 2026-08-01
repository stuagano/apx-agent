"""Field-description curation helpers for the dev UI (#292 phase A).

Assemble the per-column "current vs suggested" curation state for an agent's OKF
bundle, and write accepted descriptions back into the bundle. The suggestion
source is Unity Catalog COMMENTs (the same source ``apx-agent agents
pull-comments`` uses).

Writing accepted descriptions edits the LOCAL OKF bundle (authoring — same trust
model as editing ``agent_router.py``); it does NOT write to Unity Catalog, so it
needs no governed-write path.

Also owns the empty-state **Generate pack** path: when a DataAgent declares a
catalog.schema but the project has no ``.apx/okf/`` yet, Grounding can emit the
pack from Unity Catalog (Tables API) and wire ``knowledge = "./.apx/okf"``.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._okf import apply_uc_comments, dump_schema_cache, okf_columns, okf_manifest, write_okf_bundle
from ._schema import APX_DIR, introspect_schema_columns, load_baked_schema

logger = logging.getLogger(__name__)

_KNOWLEDGE_TOML = 'knowledge = "./.apx/okf"'


def resolve_okf_root(start: "Path | str | None" = None) -> "Path | None":
    """First ``.apx/okf`` bundle directory walking up from ``start`` (cwd by
    default), or ``None`` when the project has no OKF bundle."""
    here = (Path(start) if start is not None else Path.cwd()).resolve()
    for d in [here, *here.parents]:
        okf_root = d / APX_DIR / "okf"
        if okf_root.is_dir():
            return okf_root
    return None


def resolve_project_root(start: "Path | str | None" = None) -> "Path | None":
    """Directory containing ``pyproject.toml``, walking up from ``start`` (cwd
    by default), then falling back to the agent-module deploy root."""
    here = (Path(start) if start is not None else Path.cwd()).resolve()
    for d in [here, *here.parents]:
        if (d / "pyproject.toml").is_file():
            return d
    try:
        from ._ui_edit import _find_deploy_root

        return _find_deploy_root()
    except Exception:
        return None


def resolve_data_source_for_grounding(
    ctx: "Any | None" = None,
    start: "Path | str | None" = None,
) -> "tuple[str, str] | None":
    """Best-effort ``(catalog, schema)`` for Generate-pack when no OKF exists.

    Order: live leaf ``.catalog/.schema`` → ``APX_CATALOG``/``APX_SCHEMA`` →
    ``agent.py`` AST → baked ``.apx/schema.json``.
    """
    agent = getattr(ctx, "agent", None) if ctx is not None else None
    if agent is not None:
        try:
            from ._discover_hot import resolve_live_leaf

            leaf = resolve_live_leaf(agent, "agent") or agent
        except Exception:
            leaf = agent
        cat = getattr(leaf, "catalog", None) or ""
        sch = getattr(leaf, "schema", None) or ""
        if isinstance(cat, str) and isinstance(sch, str) and cat and sch:
            return cat, sch

    env_cat = (os.environ.get("APX_CATALOG") or "").strip()
    env_sch = (os.environ.get("APX_SCHEMA") or "").strip()
    if env_cat and env_sch and "$" not in env_cat and "$" not in env_sch:
        return env_cat, env_sch

    root = resolve_project_root(start)
    if root is not None:
        from ._doctor import _data_source_from_agent_py

        pair = _data_source_from_agent_py(root)
        if pair:
            return pair

    baked = load_baked_schema(root) if root is not None else load_baked_schema()
    if (
        baked
        and isinstance(baked.get("catalog"), str)
        and isinstance(baked.get("schema"), str)
        and baked["catalog"]
        and baked["schema"]
    ):
        return baked["catalog"], baked["schema"]
    return None


def grounding_columns_payload(
    okf_root: "Path | None",
    ws: "Any | None",
    ctx: "Any | None" = None,
) -> dict[str, Any]:
    """Shape for ``GET /_apx/grounding/columns`` — curation state, or empty-state
    Generate-pack metadata when no bundle exists."""
    if okf_root is not None:
        data = build_column_curation(okf_root, ws)
        data["can_generate"] = False
        data["generate_from"] = ""
        return data
    pair = resolve_data_source_for_grounding(ctx)
    if pair:
        catalog, schema = pair
        return {
            "catalog": catalog,
            "schema": schema,
            "tables": [],
            "can_generate": True,
            "generate_from": f"{catalog}.{schema}",
        }
    return {
        "catalog": "",
        "schema": "",
        "tables": [],
        "can_generate": False,
        "generate_from": "",
    }


def ensure_knowledge_in_pyproject(project_root: Path) -> bool:
    """Insert ``knowledge = "./.apx/okf"`` under ``[tool.apx.agent]`` when absent.

    Returns ``True`` when the file was modified.
    """
    path = project_root / "pyproject.toml"
    if not path.is_file():
        return False
    text = path.read_text()
    if re.search(r"(?m)^\s*knowledge\s*=", text):
        return False
    m = re.search(r"(?m)^\[tool\.apx\.agent\]\s*$", text)
    if not m:
        return False
    insert_at = m.end()
    updated = text[:insert_at] + f"\n{_KNOWLEDGE_TOML}" + text[insert_at:]
    path.write_text(updated)
    return True


def ensure_knowledge_in_agent_py(project_root: Path) -> bool:
    """Add ``knowledge="./.apx/okf"`` to the first DataAgent/CoworkerAgent call.

    Best-effort AST rewrite; returns ``True`` when the file was modified.
    """
    import ast

    path = project_root / "agent.py"
    if not path.is_file():
        return False
    src = path.read_text()
    if re.search(r"\bknowledge\s*=", src):
        return False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    targets = {"DataAgent", "CoworkerAgent"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            fname = fn.id
        elif isinstance(fn, ast.Attribute):
            fname = fn.attr
        else:
            continue
        if fname not in targets:
            continue
        if any(k.arg == "knowledge" for k in node.keywords if k.arg):
            return False
        # Prefer splicing after an existing ``name=`` kwarg; else before the
        # closing paren of the call.
        name_kw = next((k for k in node.keywords if k.arg == "name"), None)
        insert = ', knowledge="./.apx/okf"'
        if name_kw is not None and getattr(name_kw, "end_lineno", None):
            # end_col_offset is past the value expression
            lines = src.splitlines(keepends=True)
            lineno = name_kw.end_lineno or name_kw.lineno
            col = name_kw.end_col_offset or 0
            # Convert to absolute offset
            offset = sum(len(lines[i]) for i in range(lineno - 1)) + col
            updated = src[:offset] + insert + src[offset:]
        else:
            end = getattr(node, "end_lineno", None)
            end_col = getattr(node, "end_col_offset", None)
            if end is None or end_col is None:
                return False
            lines = src.splitlines(keepends=True)
            # Insert before the closing ')'
            offset = sum(len(lines[i]) for i in range(end - 1)) + end_col - 1
            updated = src[:offset] + insert + src[offset:]
        try:
            compile(updated, str(path), "exec")
        except SyntaxError:
            return False
        path.write_text(updated)
        return True
    return False


def generate_okf_pack(
    ws: Any,
    catalog: str,
    schema: str,
    *,
    project_root: "Path | None" = None,
    force: bool = False,
) -> dict[str, Any]:
    """Emit ``.apx/okf`` + ``schema.json`` from UC Tables API for ``catalog.schema``.

    Seeds column descriptions from UC COMMENTs when present. Wires
    ``knowledge = "./.apx/okf"`` into ``pyproject.toml`` (and ``agent.py`` when
    possible). Returns a result dict; raises ``ValueError`` for user-facing
    failures (no tables, pack exists without force, no project root).
    """
    root = project_root or resolve_project_root()
    if root is None:
        raise ValueError("Could not find project root (pyproject.toml)")
    catalog = (catalog or "").strip()
    schema = (schema or "").strip()
    if not catalog or not schema:
        raise ValueError("catalog and schema are required")

    okf_root = root / APX_DIR / "okf"
    if okf_root.is_dir() and not force:
        raise ValueError(f".apx/okf already exists at {okf_root} (pass force to overwrite)")

    tables = introspect_schema_columns(ws, catalog, schema)
    if not tables:
        raise ValueError(
            f"No tables found for {catalog}.{schema} — check Unity Catalog grants "
            "and that the schema is not empty"
        )

    descriptions = fetch_uc_comments(ws, catalog, schema) if ws is not None else {}
    # fetch_uc_comments maps {table: {col: comment}}; blank comments are fine
    # for write_okf_bundle (it uses them as Description cells).
    manifest = {"catalog": catalog, "schema": schema, "tables": tables}
    ts = datetime.now(timezone.utc).isoformat()
    write_okf_bundle(manifest, okf_root, timestamp=ts, descriptions=descriptions or None)

    apx = root / APX_DIR
    apx.mkdir(parents=True, exist_ok=True)
    regen = okf_manifest(okf_root) or manifest
    (apx / "schema.json").write_text(dump_schema_cache(regen))

    knowledge_wired = ensure_knowledge_in_pyproject(root)
    agent_wired = ensure_knowledge_in_agent_py(root)

    return {
        "ok": True,
        "catalog": catalog,
        "schema": schema,
        "table_count": len(tables),
        "okf_root": str(okf_root),
        "knowledge_wired": knowledge_wired or agent_wired,
        "restart_required": True,
    }


def fetch_uc_comments(ws: Any, catalog: str, schema: str) -> dict[str, dict[str, str]]:
    """``{table: {col: comment}}`` from Unity Catalog. Totalised — returns ``{}``
    on any failure, so the curation view degrades to current-only (no
    suggestions) rather than erroring."""
    out: dict[str, dict[str, str]] = {}
    try:
        for t in ws.tables.list(catalog_name=catalog, schema_name=schema):
            tname = getattr(t, "name", None)
            if not tname:
                continue
            cmap: dict[str, str] = {}
            for c in (getattr(t, "columns", None) or []):
                cname = getattr(c, "name", None)
                if cname:
                    cmap[cname] = getattr(c, "comment", None) or ""
            out[tname] = cmap
    except Exception as e:
        logger.warning("fetch_uc_comments failed for %s.%s: %s", catalog, schema, e)
        return {}
    return out


def build_column_curation(okf_root: "Path", ws: "Any | None") -> dict[str, Any]:
    """Assemble the curation state: ``{catalog, schema, tables: [{table,
    columns: [{column, type, current, suggested}]}]}``.

    ``suggested`` is the UC comment when it is non-empty and differs from the
    current OKF description (else ``""``). ``ws=None`` or a UC failure leaves all
    suggestions empty — the current descriptions still render.
    """
    manifest = okf_manifest(okf_root) or {}
    catalog = manifest.get("catalog", "")
    schema = manifest.get("schema", "")
    cols_by_table = okf_columns(okf_root)
    uc = fetch_uc_comments(ws, catalog, schema) if (ws and catalog and schema) else {}
    tables: list[dict[str, Any]] = []
    for table, rows in cols_by_table.items():
        uc_cols = uc.get(table, {})
        columns = [
            {
                "column": r["name"],
                "type": r["type"],
                "current": r["description"],
                "suggested": (
                    uc_cols.get(r["name"], "")
                    if uc_cols.get(r["name"], "") and uc_cols.get(r["name"]) != r["description"]
                    else ""
                ),
            }
            for r in rows
        ]
        tables.append({"table": table, "columns": columns})
    return {"catalog": catalog, "schema": schema, "tables": tables}


def apply_column_descriptions(okf_root: "Path", accepted: dict[str, dict[str, str]]) -> int:
    """Write accepted ``{table: {col: description}}`` into the OKF bundle's
    ``# Schema`` Description cells (overwriting). Returns the number of tables
    modified. Blank descriptions are no-ops (you reject by not accepting)."""
    return apply_uc_comments(okf_root, accepted, overwrite=True)


# ── Phase B: the dev-UI curation panel ───────────────────────────────────────
#
# Raw template with a ``{{NAV}}`` placeholder so the CSS/JS braces don't need
# f-string escaping. The page fetches GET /_apx/grounding/columns, lets the user
# edit/accept per-column descriptions, and POSTs the changed ones back.

_GROUNDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Grounding — APX Dev</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d0d0d; color: #e8e8e8; min-height: 100vh; }
  header { padding: 12px 20px; background: #111; border-bottom: 1px solid #2a2a2a;
           display: flex; align-items: center; gap: 12px; }
  .badge { background: #1e3a5f; color: #60b0ff; font-size: 11px; font-weight: 600;
           padding: 2px 8px; border-radius: 4px; letter-spacing: .5px; text-transform: uppercase; }
  h1 { font-size: 16px; font-weight: 600; color: #fff; }
  nav { display: flex; gap: 4px; margin-left: auto; }
  nav a { font-size: 12px; color: #888; text-decoration: none; padding: 3px 10px;
          border-radius: 5px; border: 1px solid transparent; }
  nav a:hover { color: #ccc; border-color: #333; }
  nav a.active { color: #60b0ff; background: #0d1f38; border-color: #1e3a5f; }
  main { padding: 28px 40px; max-width: 920px; }
  p.desc { color: #666; font-size: 13px; margin-bottom: 22px; line-height: 1.6; }
  h2.tbl { font-size: 13px; color: #9bf; margin: 22px 0 8px; font-family: monospace; }
  .col { display: grid; grid-template-columns: 220px 1fr auto; gap: 10px;
         align-items: start; padding: 6px 0; border-top: 1px solid #1a1a1a; }
  .col .name { font-family: monospace; font-size: 12px; color: #ccc; padding-top: 7px; }
  .col .name .ty { color: #555; }
  .col textarea { width: 100%; background: #1a1a1a; border: 1px solid #333; color: #e8e8e8;
                  border-radius: 6px; padding: 6px 9px; font-size: 12px; resize: vertical;
                  min-height: 34px; outline: none; font-family: inherit; }
  .col textarea:focus { border-color: #3a7bd5; }
  .col textarea.changed { border-color: #4ade80; }
  .sug { font-size: 11px; color: #888; margin-top: 3px; }
  .sug[hidden] { display: none; }
  .sug b { color: #aaa; font-weight: 600; }
  .thead { display: flex; align-items: center; gap: 12px; margin: 22px 0 8px; }
  .thead h2.tbl { margin: 0; }
  .suggest { background: #1a1040; color: #a78bfa; border: 1px solid #4c1d95;
             border-radius: 5px; padding: 4px 10px; font-size: 11px; cursor: pointer; }
  .suggest:hover { background: #2d1b69; }
  .suggest:disabled { opacity: .5; cursor: default; }
  .accept { background: #0d2818; color: #4ade80; border: 1px solid #1d5a3a;
            border-radius: 5px; padding: 5px 10px; font-size: 11px; cursor: pointer; white-space: nowrap; }
  .accept[hidden] { display: none; }
  .accept:hover { background: #11401f; }
  #bar { position: sticky; bottom: 0; background: #0d0d0d; border-top: 1px solid #222;
         padding: 12px 0; margin-top: 18px; display: flex; align-items: center; gap: 14px; }
  #save { background: #2563eb; color: #fff; border: none; border-radius: 6px;
          padding: 7px 18px; font-size: 13px; font-weight: 600; cursor: pointer; }
  #save:hover { background: #1d4ed8; }
  #save:disabled { opacity: .5; cursor: default; }
  #status { font-size: 12px; color: #666; }
  #status.ok { color: #4ade80; }
  #status.err { color: #f87171; }
  .empty { margin-top: 8px; }
  .empty .cta { background: #2563eb; color: #fff; border: none; border-radius: 6px;
                padding: 8px 16px; font-size: 13px; font-weight: 600; cursor: pointer; margin-top: 12px; }
  .empty .cta:hover { background: #1d4ed8; }
  .empty .cta:disabled { opacity: .5; cursor: default; }
  .empty code { font-size: 12px; color: #9bf; }
  #bar[hidden] { display: none; }
</style>
</head>
<body>
<header>
  <span class="badge">APX dev</span>
  <h1>Grounding</h1>
  <nav>{{NAV}}</nav>
</header>
<main>
  <p class="desc">Review and curate the per-column descriptions in your OKF grounding bundle.
  Suggestions come from Unity Catalog COMMENTs. Edits are saved to the local bundle (not Unity Catalog).</p>
  <div id="tables"></div>
  <div id="bar" hidden>
    <button id="save">Save changes</button>
    <span id="status"></span>
  </div>
</main>
<script>
const esc = (s) => (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function load() {
  const status = document.getElementById('status');
  let data;
  try {
    const r = await fetch('/_apx/grounding/columns');
    data = await r.json();
  } catch (e) { status.textContent = 'Failed to load: ' + e.message; status.className = 'err'; return; }
  render(data);
}

const cssEsc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;

function setSug(row, text) {
  const sug = row.querySelector('.sug'), btn = row.querySelector('.accept');
  if (!text) { sug.hidden = true; btn.hidden = true; return; }
  sug.innerHTML = '<b>Suggested:</b> ' + esc(text); sug.hidden = false;
  btn.dataset.sug = text; btn.hidden = false;
}

async function generatePack(btn, from) {
  const status = document.getElementById('status');
  const bar = document.getElementById('bar');
  bar.hidden = false;
  btn.disabled = true; btn.textContent = 'Generating…';
  status.textContent = 'Generating pack from ' + from + '…'; status.className = '';
  try {
    const r = await fetch('/_apx/grounding/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    status.textContent = `✓ Pack created (${d.table_count} table${d.table_count === 1 ? '' : 's'}). Restart the agent to load grounding.`;
    status.className = 'ok';
    await load();
  } catch (e) {
    status.textContent = '✗ ' + e.message; status.className = 'err';
    btn.disabled = false; btn.textContent = 'Generate pack from ' + from;
  }
}

function render(data) {
  const root = document.getElementById('tables');
  const bar = document.getElementById('bar');
  if (!data.tables || !data.tables.length) {
    bar.hidden = !data.can_generate;
    if (data.can_generate && data.generate_from) {
      root.innerHTML =
        '<div class="empty">' +
        '<p class="desc">No OKF grounding pack yet. Every DataAgent should have one for ' +
        '<code>' + esc(data.generate_from) + '</code> — generate it from Unity Catalog.</p>' +
        '<button class="cta" id="gen">Generate pack from ' + esc(data.generate_from) + '</button>' +
        '</div>';
      document.getElementById('gen').addEventListener('click', (e) =>
        generatePack(e.currentTarget, data.generate_from));
    } else {
      root.innerHTML = '<p class="desc">No OKF grounding bundle found. Point a DataAgent at a catalog.schema, then generate a pack here.</p>';
    }
    return;
  }
  bar.hidden = false;
  root.innerHTML = '';
  for (const t of data.tables) {
    const head = document.createElement('div'); head.className = 'thead';
    head.innerHTML = `<h2 class="tbl">${esc(t.table)}</h2>` +
      `<button class="suggest" data-table="${esc(t.table)}">✨ Suggest</button>`;
    root.appendChild(head);
    for (const c of t.columns) {
      const row = document.createElement('div'); row.className = 'col';
      row.innerHTML =
        `<div class="name">${esc(c.column)} <span class="ty">${esc(c.type)}</span></div>` +
        `<div><textarea data-table="${esc(t.table)}" data-col="${esc(c.column)}" ` +
        `data-current="${esc(c.current)}">${esc(c.current)}</textarea>` +
        `<div class="sug" hidden></div></div>` +
        `<button class="accept" hidden>Accept</button>`;
      root.appendChild(row);
      if (c.suggested) setSug(row, c.suggested);
    }
  }
  root.querySelectorAll('.accept').forEach(btn => btn.addEventListener('click', () => {
    const ta = btn.closest('.col').querySelector('textarea');
    ta.value = btn.dataset.sug; ta.classList.add('changed');
  }));
  root.querySelectorAll('textarea').forEach(ta => ta.addEventListener('input', () => {
    ta.classList.toggle('changed', ta.value !== ta.dataset.current);
  }));
  root.querySelectorAll('.suggest').forEach(btn =>
    btn.addEventListener('click', () => suggestTable(btn.dataset.table, btn)));
}

async function suggestTable(table, btn) {
  const status = document.getElementById('status');
  btn.disabled = true; btn.textContent = '✨ …';
  try {
    const r = await fetch('/_apx/grounding/suggest', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ table }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    let n = 0;
    for (const [col, desc] of Object.entries(d.suggestions || {})) {
      const ta = document.querySelector(
        `#tables textarea[data-table="${cssEsc(table)}"][data-col="${cssEsc(col)}"]`);
      if (ta && desc) { setSug(ta.closest('.col'), desc); n++; }
    }
    status.textContent = `✨ ${n} AI suggestion${n === 1 ? '' : 's'} for ${table}`;
    status.className = '';
  } catch (e) {
    status.textContent = '✗ ' + e.message; status.className = 'err';
  } finally { btn.disabled = false; btn.textContent = '✨ Suggest'; }
}

async function save() {
  const btn = document.getElementById('save'), status = document.getElementById('status');
  const accepted = {};
  document.querySelectorAll('#tables textarea').forEach(ta => {
    const val = ta.value.trim();
    if (val && val !== ta.dataset.current) {
      (accepted[ta.dataset.table] = accepted[ta.dataset.table] || {})[ta.dataset.col] = val;
    }
  });
  if (!Object.keys(accepted).length) { status.textContent = 'No changes to save.'; status.className = ''; return; }
  btn.disabled = true; status.textContent = 'Saving…'; status.className = '';
  try {
    const r = await fetch('/_apx/grounding/columns', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ accepted }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    status.textContent = `✓ Saved (${d.modified} table${d.modified === 1 ? '' : 's'} updated)`;
    status.className = 'ok';
    load();  // re-pull so accepted suggestions become the new current
  } catch (e) {
    status.textContent = '✗ ' + e.message; status.className = 'err';
  } finally { btn.disabled = false; }
}

document.getElementById('save').addEventListener('click', save);
load();
</script>
</body>
</html>
"""


def render_grounding_ui() -> str:
    """The field-description curation page (#292 phase B). Fetches
    ``/_apx/grounding/columns``, lets the user edit/accept per-column
    descriptions, and POSTs the changed ones back."""
    from ._ui_nav import _apx_nav_links

    return _GROUNDING_HTML.replace("{{NAV}}", _apx_nav_links("grounding"))


async def generate_column_descriptions(
    model: str, table: str, rows: "list[dict]"
) -> dict[str, str]:
    """LLM-generate a one-sentence description per column (#292 phase C) — an
    AI suggestion source alongside UC comments (the BigQuery-Gemini parity).

    ``rows`` is ``[{name, type, ...}]``. Returns ``{column: description}`` for the
    known columns only; totalised — ``{}`` on import/LLM/parse failure.
    """
    import json as _json

    if not rows or not model:
        return {}
    cols_txt = "\n".join(f"- {r['name']} ({r.get('type', '')})" for r in rows)
    system_msg = (
        "You write concise one-sentence descriptions for database table columns, "
        "as grounding metadata for a data agent. Output ONLY a JSON object mapping "
        "each column name to its description — no prose, no code fences."
    )
    user_content = f"Table: {table}\nColumns:\n{cols_txt}\n\nDescribe each column."
    try:
        from databricks_openai import AsyncDatabricksOpenAI

        llm = AsyncDatabricksOpenAI()
        resp = await llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_content},
            ],
            max_tokens=600,
            temperature=0.0,
        )
        choices = getattr(resp, "choices", None) or []
        raw = ""
        if choices:
            msg = getattr(choices[0], "message", None)
            raw = ((getattr(msg, "content", None) or "") if msg else "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = _json.loads(raw)
        if not isinstance(data, dict):
            return {}
        names = {r["name"] for r in rows}
        return {k: str(v) for k, v in data.items() if k in names and v}
    except Exception as e:
        logger.warning("generate_column_descriptions failed for %s: %s", table, e)
        return {}
