"""Shared Du Bois design tokens + component CSS for the /_apx/* dev UI.

Color values are copied verbatim from @databricks/design-system
(dubois-colors.less). The dev UI is server-rendered HTML with no npm/build, so
we adopt Du Bois *values*, not its React components. Dark-only for now; the vars
are structured so a light theme could be layered on later.

Injected onto every /_apx/* page via ``_ui_nav._deploy_overlay_html``.
"""

from __future__ import annotations

# Du Bois greys/accents (dubois-colors.less): grey800/700/650/600, grey100,
# grey350, blue600/500, green500, coral, lemon.
APX_THEME_CSS = """
  /* apx-dubois-theme */
  :root{
    --apx-bg:#11171C; --apx-panel:#1F272D; --apx-panel-2:#37444F;
    --apx-border:#445461; --apx-text:#E8ECF0; --apx-muted:#92A4B3;
    --apx-accent:#2272B4; --apx-accent-hover:#4299E0;
    --apx-ok:#3BA65E; --apx-err:#C83243; --apx-warn:#FACB66;
    --apx-on-accent:#FFFFFF;
    --apx-font:"DM Sans",ui-sans-serif,system-ui,-apple-system,sans-serif;
    --apx-mono:"DM Mono",ui-monospace,monospace;
  }
  body{font-family:var(--apx-font);background:var(--apx-bg);color:var(--apx-text);}
"""

# Shared primitives the per-module pages hand-rolled today. Migration collapses
# each module's local .card/.badge/.btn/.empty/pre onto these. Tokens only — no
# hex literals (enforced by test_shared_components_defined_with_tokens).
APX_COMPONENTS_CSS = """
  .apx-card{background:var(--apx-panel);border:1px solid var(--apx-border);
            border-radius:10px;padding:12px 14px;}
  .apx-badge{background:var(--apx-panel-2);color:var(--apx-accent);font-size:11px;
             font-weight:600;padding:2px 8px;border-radius:4px;letter-spacing:.5px;
             text-transform:uppercase;}
  .apx-btn{background:transparent;color:var(--apx-muted);
           border:1px solid var(--apx-border);border-radius:6px;padding:5px 14px;
           font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;}
  .apx-btn:hover{color:var(--apx-text);border-color:var(--apx-accent);}
  .apx-btn:disabled{opacity:.5;cursor:default;}
  .apx-btn.primary{background:var(--apx-accent);color:var(--apx-on-accent);
                   border-color:var(--apx-accent);}
  .apx-btn.primary:hover{background:var(--apx-accent-hover);}
  .apx-btn.danger{color:var(--apx-err);border-color:var(--apx-err);}
  .apx-empty{color:var(--apx-muted);padding:32px 10px;font-style:italic;}
  .apx-pre{background:var(--apx-bg);border:1px solid var(--apx-border);
           border-radius:5px;padding:8px 10px;font-size:11px;
           font-family:var(--apx-mono);color:var(--apx-muted);
           white-space:pre-wrap;word-break:break-all;max-height:240px;
           overflow-y:auto;}
"""


def apx_theme_style() -> str:
    """The dev UI theme as one <style> block, injected on every /_apx/* page."""
    return f"<style>{APX_THEME_CSS}\n{APX_COMPONENTS_CSS}</style>"
