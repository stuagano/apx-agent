"""Knowledge tab rendering for the dev UI — displays OKF bundle contents."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def render_knowledge_ui() -> str:
    """The Knowledge page — renders OKF bundle Functions, Views, and Glossary.

    Returns a self-contained HTML page. Totalized: returns empty-state page if
    no OKF bundle is found, never raises.
    """
    from ._ui_grounding import resolve_okf_root
    from ._okf import okf_grounding, okf_glossary, OKFDocument

    okf_root = resolve_okf_root()

    # Parse OKF bundle (okf_root is None when no bundle is found -> empty state)
    views_data = (okf_grounding(okf_root) or {}) if okf_root else {}
    glossary_data = (okf_glossary(okf_root) or []) if okf_root else []
    functions_data = []

    # Parse functions from functions/*.md if they exist
    if okf_root:
        functions_dir = okf_root / "functions"
        if functions_dir.is_dir():
            for func_md in sorted(functions_dir.glob("*.md")):
                try:
                    doc = OKFDocument.parse(func_md.read_text())
                    title = doc.frontmatter.get("title", func_md.stem)
                    description = doc.frontmatter.get("description", "")

                    # Extract sections from body
                    from ._okf import _extract_section
                    overview = _extract_section(doc.body, "Overview").strip()
                    parameters_sec = _extract_section(doc.body, "Parameters").strip()
                    returns_sec = _extract_section(doc.body, "Returns").strip()
                    examples_sec = _extract_section(doc.body, "Examples").strip()
                    synonyms_sec = _extract_section(doc.body, "Synonyms").strip()

                    functions_data.append({
                        "name": title,
                        "description": description,
                        "overview": overview,
                        "parameters": parameters_sec,
                        "returns": returns_sec,
                        "examples": examples_sec,
                        "synonyms": synonyms_sec,
                    })
                except Exception as e:
                    logger.debug(f"Failed to parse function {func_md.name}: {e}")
                    continue

    return _render_knowledge_html(functions_data, views_data, glossary_data)


def _render_knowledge_html(
    functions: list[dict[str, str]],
    views: dict[str, Any],
    glossary: list[dict[str, Any]],
) -> str:
    """Render the Knowledge page HTML with Functions, Views, and Glossary.

    Uses the same dark theme and CSS patterns as _ui_grounding.py.
    """
    from ._ui_nav import _apx_nav_links

    nav_links = _apx_nav_links("knowledge")

    # HTML-escape helper
    def esc(s: str) -> str:
        if not s:
            return ""
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # Render functions section
    functions_html = ""
    if functions:
        functions_html = '<section class="functions-section">\n'
        functions_html += '<h2 class="section-title">Functions</h2>\n'
        for func in functions:
            functions_html += f'''<div class="function-card">
  <div class="function-name">{esc(func["name"])}</div>
  <div class="function-desc">{esc(func["description"])}</div>
  <div class="function-content">
'''
            if func.get("overview"):
                functions_html += f'    <div class="subsection"><strong>Overview:</strong> {esc(func["overview"])}</div>\n'
            if func.get("parameters"):
                functions_html += f'    <div class="subsection"><strong>Parameters:</strong>\n      <pre>{esc(func["parameters"])}</pre>\n    </div>\n'
            if func.get("returns"):
                functions_html += f'    <div class="subsection"><strong>Returns:</strong> {esc(func["returns"])}</div>\n'
            if func.get("examples"):
                functions_html += f'    <div class="subsection"><strong>Example:</strong>\n      <pre class="example-block">{esc(func["examples"][:200])}</pre>\n    </div>\n'
            functions_html += '''  </div>
</div>
'''
        functions_html += '</section>\n'

    # Render views section
    views_html = ""
    if views:
        views_html = '<section class="views-section">\n'
        views_html += '<h2 class="section-title">Views</h2>\n'
        for view_name, view_data in views.items():
            views_html += f'''<div class="view-card">
  <div class="view-name">{esc(view_name)}</div>
'''
            if view_data.get("description"):
                views_html += f'  <div class="view-desc">{esc(view_data["description"])}</div>\n'

            # Columns table
            if view_data.get("columns"):
                views_html += '''  <table class="columns-table">
    <thead><tr><th>Column</th><th>Type</th><th>Description</th></tr></thead>
    <tbody>
'''
                for col in view_data["columns"]:
                    views_html += f'''      <tr>
        <td><code>{esc(col.get("name", ""))}</code></td>
        <td><code>{esc(col.get("type", ""))}</code></td>
        <td>{esc(col.get("description", ""))}</td>
      </tr>
'''
                views_html += '''    </tbody>
  </table>
'''

            # Golden query example
            if view_data.get("golden_queries") and view_data["golden_queries"]:
                gq = view_data["golden_queries"][0]
                views_html += f'''  <div class="golden-query">
    <strong>Example Query:</strong>
    <pre><code>{esc(gq.get("sql", "")[:300])}</code></pre>
  </div>
'''
            views_html += '''</div>
'''
        views_html += '</section>\n'

    # Render glossary section
    glossary_html = ""
    if glossary:
        glossary_html = '<section class="glossary-section">\n'
        glossary_html += '<h2 class="section-title">Glossary</h2>\n'
        glossary_html += '<div class="glossary-entries">\n'
        for entry in glossary:
            glossary_html += f'''<div class="glossary-entry">
  <div class="term">{esc(entry.get("term", ""))}</div>
  <div class="definition">{esc(entry.get("definition", ""))}</div>
'''
            if entry.get("synonyms"):
                syn_chips = " ".join(f'<span class="synonym-chip">{esc(s)}</span>' for s in entry["synonyms"])
                glossary_html += f'  <div class="synonyms">{syn_chips}</div>\n'
            glossary_html += '''</div>
'''
        glossary_html += '</div>\n</section>\n'

    # Empty state
    empty_state = ""
    if not functions and not views and not glossary:
        empty_state = '''<div class="empty-state">
  <p>No OKF knowledge bundle found. Create a <code>.apx/okf</code> bundle to populate this view.</p>
</div>
'''

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Knowledge — APX Dev</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d0d0d; color: #e8e8e8; min-height: 100vh; }}
  header {{ padding: 12px 20px; background: #111; border-bottom: 1px solid #2a2a2a;
           display: flex; align-items: center; gap: 12px; }}
  .badge {{ background: #1e3a5f; color: #60b0ff; font-size: 11px; font-weight: 600;
           padding: 2px 8px; border-radius: 4px; letter-spacing: .5px; text-transform: uppercase; }}
  h1 {{ font-size: 16px; font-weight: 600; color: #fff; }}
  nav {{ display: flex; gap: 4px; margin-left: auto; }}
  nav a {{ font-size: 12px; color: #888; text-decoration: none; padding: 3px 10px;
          border-radius: 5px; border: 1px solid transparent; }}
  nav a:hover {{ color: #ccc; border-color: #333; }}
  nav a.active {{ color: #60b0ff; background: #0d1f38; border-color: #1e3a5f; }}
  main {{ padding: 28px 40px; max-width: 1200px; }}

  .section-title {{ font-size: 14px; color: #9bf; margin: 24px 0 12px; font-weight: 600; }}

  /* Functions */
  .function-card {{ margin-bottom: 16px; padding: 12px; background: #0f0f0f;
                    border: 1px solid #1a1a1a; border-radius: 6px; }}
  .function-name {{ font-family: monospace; font-size: 13px; font-weight: 600;
                   color: #a78bfa; margin-bottom: 4px; }}
  .function-desc {{ font-size: 12px; color: #aaa; margin-bottom: 8px; }}
  .function-content {{ font-size: 12px; line-height: 1.5; }}
  .subsection {{ margin-top: 6px; padding: 6px; background: #0a0a0a;
                border-left: 2px solid #4c1d95; padding-left: 10px; }}
  pre {{ font-size: 11px; overflow-x: auto; white-space: pre-wrap; word-break: break-word;
       color: #8b949e; background: #0a0a0a; padding: 4px 6px; border-radius: 3px; }}
  .example-block {{ max-height: 80px; }}

  /* Views */
  .view-card {{ margin-bottom: 20px; padding: 14px; background: #0f0f0f;
               border: 1px solid #1a1a1a; border-radius: 6px; }}
  .view-name {{ font-family: monospace; font-size: 13px; font-weight: 600;
               color: #4ade80; margin-bottom: 6px; }}
  .view-desc {{ font-size: 12px; color: #aaa; margin-bottom: 10px; }}
  .columns-table {{ width: 100%; border-collapse: collapse; font-size: 11px;
                   margin: 10px 0; }}
  .columns-table th {{ background: #0a0a0a; padding: 6px; text-align: left;
                      color: #60b0ff; font-weight: 600; border-bottom: 1px solid #1a1a1a; }}
  .columns-table td {{ padding: 5px 6px; border-bottom: 1px solid #0a0a0a;
                      color: #8b949e; }}
  .columns-table code {{ font-size: 10px; color: #7dd3fc; }}
  .golden-query {{ margin-top: 10px; padding: 8px; background: #0a0a0a;
                  border-left: 2px solid #4ade80; padding-left: 10px; }}

  /* Glossary */
  .glossary-entries {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                      gap: 12px; }}
  .glossary-entry {{ padding: 10px; background: #0f0f0f; border: 1px solid #1a1a1a;
                    border-radius: 6px; }}
  .term {{ font-family: monospace; font-size: 12px; font-weight: 600;
          color: #f87171; margin-bottom: 4px; }}
  .definition {{ font-size: 12px; color: #aaa; line-height: 1.4; margin-bottom: 6px; }}
  .synonyms {{ display: flex; flex-wrap: wrap; gap: 4px; }}
  .synonym-chip {{ display: inline-block; background: #1a1f2e; color: #60b0ff;
                  font-size: 10px; padding: 2px 6px; border-radius: 3px;
                  border: 1px solid #4c1d95; }}

  .empty-state {{ margin-top: 40px; padding: 40px; text-align: center;
                 background: #0f0f0f; border: 1px solid #1a1a1a; border-radius: 6px;
                 color: #666; }}
  .empty-state code {{ color: #9bf; }}
</style>
</head>
<body>
<header>
  <span class="badge">APX dev</span>
  <h1>Knowledge</h1>
  <nav>{nav_links}</nav>
</header>
<main>
  {empty_state}
  {functions_html}
  {views_html}
  {glossary_html}
</main>
</body>
</html>
"""

    return html
