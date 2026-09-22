"""Shared table renderer for tool outputs across Tools, Chat, and Trace views."""

from __future__ import annotations

import html as _html
import json as _json
from typing import Any

TABLE_CSS = r"""
  .outmeta { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; white-space:normal; }
  .outmeta .mchip { font-size:11px; padding:2px 8px; border-radius:10px;
                                border:1px solid #262626; background:#0e0e0e; color:var(--muted); }
  table.out { width:100%; border-collapse:collapse; font-size:11.5px; white-space:normal; }
  table.out th { text-align:left; color:var(--muted); font-weight:600; padding:6px 8px;
                 border-bottom:1px solid #2a2a2a; white-space:nowrap;
                 position:sticky; top:0; background:#0b0b0b; }
  table.out td { padding:5px 8px; border-bottom:1px solid #161616; vertical-align:top;
                 max-width:340px; word-break:break-word; }
  table.out tr:hover td { background:#101010; }
  .muted { color:var(--dim, #555); }
  .note { margin-top:8px; color:var(--dim, #555); font-size:11px; white-space:normal; }
  details.out-raw { margin-top:10px; background:#0d0d0d; border:1px solid #1c1c1c; border-radius:8px; }
  details.out-raw > summary { padding:7px 11px; font-size:11.5px; }
  details.out-raw .body { padding:0 11px 11px; }
  details.out-raw pre { margin:0; white-space:pre-wrap; word-break:break-word;
                            max-height:20rem; overflow:auto; font-size:11.5px; color:#c9d1d9; }
"""

TABLE_JS = r"""
const MAX_TABLE_ROWS = 200;
const MAX_TABLE_COLS = 14;
const MAX_CELL = 220;

function jsonPreview(value) {
  if (value === null || value === undefined) return '<span class="muted">—</span>';
  let text;
  if (typeof value === 'string') text = value;
  else if (typeof value === 'number' || typeof value === 'boolean') text = String(value);
  else if (Array.isArray(value)) {
    const flat = value.every((v) => v === null || ['string', 'number', 'boolean'].includes(typeof v));
    text = flat ? value.map((v) => (v === null ? 'null' : String(v))).join(', ') : JSON.stringify(value);
  } else text = JSON.stringify(value);
  if (text === '') return '<span class="muted">(empty)</span>';
  return esc(text.length > MAX_CELL ? text.slice(0, MAX_CELL) + '…' : text);
}

function tableShape(value) {
  const isRows = (a) => Array.isArray(a) && a.length &&
    a.every((r) => r && typeof r === 'object' && !Array.isArray(r));
  if (isRows(value)) return { key: '', rows: value, others: [] };
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const keys = Object.keys(value).filter((k) => isRows(value[k]));
    if (keys.length) {
      keys.sort((a, b) => value[b].length - value[a].length);
      return { key: keys[0], rows: value[keys[0]], others: keys.slice(1) };
    }
  }
  return null;
}

function scalarMeta(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  const items = [];
  Object.keys(value).forEach((k) => {
    const v = value[k];
    if (v === null || ['string', 'number', 'boolean'].includes(typeof v)) {
      if (v === '' || v === null) return;
      items.push('<span class="mchip">' + esc(k) + ' = ' + esc(String(v)) + '</span>');
    }
  });
  if (!items.length) return '';
  return '<div class="outmeta">' + items.slice(0, 8).join('') +
    (items.length > 8 ? '<span class="mchip">+' + (items.length - 8) + ' more</span>' : '') + '</div>';
}

function renderTable(shape) {
  const rows = shape.rows.slice(0, MAX_TABLE_ROWS);
  const columns = [];
  rows.forEach((r) => Object.keys(r).forEach((k) => { if (columns.indexOf(k) === -1) columns.push(k); }));
  const cols = columns.slice(0, MAX_TABLE_COLS);
  const head = cols.map((c) => '<th>' + esc(c) + '</th>').join('');
  const body = rows.map((r) => '<tr>' +
    cols.map((c) => '<td>' + jsonPreview(r[c]) + '</td>').join('') + '</tr>').join('');
  const notes = [];
  if (shape.key) notes.push('rows: ' + esc(shape.key));
  notes.push(rows.length + ' row' + (rows.length === 1 ? '' : 's'));
  if (shape.rows.length > rows.length) notes.push('+' + (shape.rows.length - rows.length) + ' more not shown');
  if (columns.length > cols.length) notes.push('+' + (columns.length - cols.length) + ' more column(s) not shown');
  if (shape.others.length) notes.push('other arrays: ' + esc(shape.others.join(', ')));
  return '<table class="out"><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table>' +
    '<div class="note">' + notes.join(' · ') + ' · full response under Raw JSON</div>';
}

function renderToolOutput(raw) {
  const text = typeof raw === 'string' ? raw : (raw === undefined ? '' : JSON.stringify(raw));
  if (!text) return '<span class="muted">(no output)</span>';
  let pretty;
  try {
    pretty = JSON.stringify(JSON.parse(text), null, 2);
  } catch (e) {
    return esc(text);
  }
  if (pretty.length > 40000) {
    return esc(pretty.slice(0, 40000) + '\n… truncated at 40,000 chars');
  }
  let value = null;
  try { value = JSON.parse(text); } catch (e) { value = null; }
  const shape = value === null ? null : tableShape(value);
  // Not tabular: the pretty JSON *is* the view — no duplicate toggle.
  if (!shape) return esc(pretty);
  const rawBlock = '<details class="out-raw"><summary>Raw JSON</summary><div class="body"><pre>' +
    esc(pretty) + '</pre></div></details>';
  return scalarMeta(value) + renderTable(shape) + rawBlock;
}
"""

_MAX_TABLE_ROWS = 200
_MAX_TABLE_COLS = 14
_MAX_CELL = 220
_MAX_RAW_JSON = 40000


def _table_shape(value: Any) -> dict[str, Any] | None:
    def is_rows(a: Any) -> bool:
        return (
            isinstance(a, list)
            and bool(a)
            and all(isinstance(r, dict) for r in a)
        )

    if is_rows(value):
        return {"key": "", "rows": value, "others": []}
    if isinstance(value, dict):
        keys = [k for k, v in value.items() if is_rows(v)]
        if keys:
            keys.sort(key=lambda k: len(value[k]), reverse=True)
            return {"key": keys[0], "rows": value[keys[0]], "others": keys[1:]}
    return None


def _coerce_tabular_root(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return _json.loads(value)
        except Exception:
            return value
    if isinstance(value, dict):
        for key in ('output', 'result'):
            inner = value.get(key)
            if isinstance(inner, str):
                try:
                    parsed = _json.loads(inner)
                except Exception:
                    continue
                if _table_shape(parsed):
                    return parsed
    return value


def _json_preview_html(value: Any) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        # Match the JS renderer: JSON booleans, not Python's True/False.
        text = 'true' if value else 'false'
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, list):
        flat = all(v is None or isinstance(v, (str, int, float, bool)) for v in value)
        text = ', '.join('null' if v is None else str(v) for v in value) if flat else _json.dumps(value)
    else:
        text = _json.dumps(value)
    if text == '':
        return '<span class="muted">(empty)</span>'
    if len(text) > _MAX_CELL:
        text = text[:_MAX_CELL] + '…'
    return _html.escape(text)


def _scalar_meta_html(value: Any) -> str:
    if not isinstance(value, dict):
        return ''
    items: list[str] = []
    for k, v in value.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            if v == '' or v is None:
                continue
            items.append(
                f'<span class="mchip">{_html.escape(str(k))} = {_html.escape(str(v))}</span>'
            )
    if not items:
        return ''
    more = ''
    if len(items) > 8:
        more = f'<span class="mchip">+{len(items) - 8} more</span>'
    return '<div class="outmeta">' + ''.join(items[:8]) + more + '</div>'


def _render_table_html(shape: dict[str, Any]) -> str:
    rows = list(shape['rows'][:_MAX_TABLE_ROWS])
    columns: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in columns:
                columns.append(key)
    cols = columns[:_MAX_TABLE_COLS]
    head = ''.join(f'<th>{_html.escape(str(c))}</th>' for c in cols)
    body_parts: list[str] = []
    for row in rows:
        tds = ''.join(f'<td>{_json_preview_html(row.get(c))}</td>' for c in cols)
        body_parts.append(f'<tr>{tds}</tr>')
    notes: list[str] = []
    if shape['key']:
        notes.append('rows: ' + _html.escape(str(shape['key'])))
    notes.append(f"{len(rows)} row{'' if len(rows) == 1 else 's'}")
    if len(shape['rows']) > len(rows):
        notes.append(f"+{len(shape['rows']) - len(rows)} more not shown")
    if len(columns) > len(cols):
        notes.append(f"+{len(columns) - len(cols)} more column(s) not shown")
    if shape['others']:
        notes.append('other arrays: ' + _html.escape(', '.join(str(x) for x in shape['others'])))
    return (
        '<table class="out"><thead><tr>' + head + '</tr></thead><tbody>' + ''.join(body_parts) + '</tbody></table>'
        + '<div class="note">' + ' · '.join(notes) + ' · full response under Raw JSON</div>'
    )


def render_tool_output_html(value: Any) -> str | None:
    """Render tabular tool output as HTML, or return ``None`` for non-tabular values."""
    coerced = _coerce_tabular_root(value)
    shape = _table_shape(coerced)
    if not shape:
        return None
    try:
        pretty = _json.dumps(coerced, indent=2)
    except Exception:
        pretty = _json.dumps(str(coerced), indent=2)
    if len(pretty) > _MAX_RAW_JSON:
        pretty = pretty[:_MAX_RAW_JSON] + chr(10) + '… truncated at 40,000 chars'
    raw_block = (
        '<details class="out-raw"><summary>Raw JSON</summary><div class="body"><pre>'
        + _html.escape(pretty)
        + '</pre></div></details>'
    )
    return _scalar_meta_html(coerced) + _render_table_html(shape) + raw_block
