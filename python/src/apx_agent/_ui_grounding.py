"""Field-description curation helpers for the dev UI (#292 phase A).

Assemble the per-column "current vs suggested" curation state for an agent's OKF
bundle, and write accepted descriptions back into the bundle. The suggestion
source is Unity Catalog COMMENTs (the same source ``apx-agent agents
pull-comments`` uses).

Writing accepted descriptions edits the LOCAL OKF bundle (authoring — same trust
model as editing ``agent_router.py``); it does NOT write to Unity Catalog, so it
needs no governed-write path.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ._okf import apply_uc_comments, okf_columns, okf_manifest
from ._schema import APX_DIR

logger = logging.getLogger(__name__)


def resolve_okf_root(start: "Path | str | None" = None) -> "Path | None":
    """First ``.apx/okf`` bundle directory walking up from ``start`` (cwd by
    default), or ``None`` when the project has no OKF bundle."""
    here = (Path(start) if start is not None else Path.cwd()).resolve()
    for d in [here, *here.parents]:
        okf_root = d / APX_DIR / "okf"
        if okf_root.is_dir():
            return okf_root
    return None


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
  .sug b { color: #aaa; font-weight: 600; }
  .accept { background: #0d2818; color: #4ade80; border: 1px solid #1d5a3a;
            border-radius: 5px; padding: 5px 10px; font-size: 11px; cursor: pointer; white-space: nowrap; }
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
  <div id="bar">
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

function render(data) {
  const root = document.getElementById('tables');
  if (!data.tables || !data.tables.length) {
    root.innerHTML = '<p class="desc">No OKF grounding bundle found in this project.</p>';
    return;
  }
  root.innerHTML = '';
  for (const t of data.tables) {
    const h = document.createElement('h2'); h.className = 'tbl'; h.textContent = t.table;
    root.appendChild(h);
    for (const c of t.columns) {
      const row = document.createElement('div'); row.className = 'col';
      const sug = c.suggested
        ? `<div class="sug"><b>Suggested:</b> ${esc(c.suggested)}</div>` : '';
      row.innerHTML =
        `<div class="name">${esc(c.column)} <span class="ty">${esc(c.type)}</span></div>` +
        `<div><textarea data-table="${esc(t.table)}" data-col="${esc(c.column)}" ` +
        `data-current="${esc(c.current)}">${esc(c.current)}</textarea>${sug}</div>` +
        (c.suggested ? `<button class="accept" data-sug="${esc(c.suggested)}">Accept</button>` : '<span></span>');
      root.appendChild(row);
    }
  }
  root.querySelectorAll('.accept').forEach(btn => btn.addEventListener('click', () => {
    const ta = btn.closest('.col').querySelector('textarea');
    ta.value = btn.dataset.sug; ta.classList.add('changed');
  }));
  root.querySelectorAll('textarea').forEach(ta => ta.addEventListener('input', () => {
    ta.classList.toggle('changed', ta.value !== ta.dataset.current);
  }));
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
