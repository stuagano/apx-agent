"""Shared design tokens + component CSS for the /_apx/* dev UI.

Values come from the vendored AgentKit chat app
(``_static/agentkit_chat/styles.css``), so every dev-UI page shares its light
look. The dev UI is server-rendered HTML with no npm/build; pages consume the
``--apx-*`` tokens, never hex literals.

Injected onto every /_apx/* page via ``_ui_nav._deploy_overlay_html``.
"""

from __future__ import annotations

# AgentKit :root — page, surface, surface-muted, line, ink, ink-soft, accent,
# accent-deep, success, danger, warning.
APX_THEME_CSS = """
  /* apx-agentkit-theme */
  :root{
    --apx-bg:#f1f3ff; --apx-panel:#ffffff; --apx-panel-2:#f7f8fc;
    --apx-border:#e7e9f0; --apx-text:#1f2124; --apx-muted:#5f6469;
    --apx-accent:#1a73e8; --apx-accent-hover:#1a5fc4;
    --apx-ok:#137333; --apx-err:#b3261e; --apx-warn:#8b5000;
    --apx-on-accent:#ffffff;
    --apx-shadow:0 1px 2px rgba(15,23,42,.04),0 1px 3px rgba(15,23,42,.03);
    --apx-font:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    --apx-mono:ui-monospace,SFMono-Regular,Menlo,monospace;
    color-scheme:light;
  }
  body{font-family:var(--apx-font);background:var(--apx-bg);color:var(--apx-text);}
"""

# Shared primitives the per-module pages hand-rolled today. Migration collapses
# each module's local .card/.badge/.btn/.empty/pre onto these. Tokens only — no
# hex literals (enforced by test_shared_components_defined_with_tokens).
APX_COMPONENTS_CSS = """
  .apx-card{background:var(--apx-panel);border:1px solid var(--apx-border);
            border-radius:12px;padding:12px 14px;box-shadow:var(--apx-shadow);}
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
