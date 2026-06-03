# Chat Markdown Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Render assistant chat messages as sanitized markdown (tables, headings, bold/italic, code, lists, links) instead of raw text, so a model's markdown table shows as a real table — not raw `|` pipes.

**Design (approved):** Vendor `marked` (markdown→HTML) + `DOMPurify` (sanitize) into `src/apx_agent/_static/vendor/` (shipped in the wheel via `packages=["src/apx_agent"]`, served locally — no CDN, offline/private-link safe). Serve them via a new dev-UI static route mirroring the existing `_static/topology` serving. In the chat page, load the libs via local `<script src>`; render **assistant** messages with `DOMPurify.sanitize(marked.parse(text))` (user messages stay plain `textContent`); re-render on each stream chunk. Add table/markdown CSS scoped to `.msg.assistant`.

**Tech Stack:** vendored marked + DOMPurify JS, FastAPI `FileResponse` static route (`_dev.py`), the dev-UI render in `_ui_chat.py`.

**Out of scope:** streaming wire-format changes; the tool-progress channel (parked, next feature); restyling non-chat dev-UI panels.

---

### Task 1: Vendor marked + DOMPurify

**Files:**
- Create: `python/src/apx_agent/_static/vendor/marked.min.js`
- Create: `python/src/apx_agent/_static/vendor/purify.min.js`

- [ ] **Step 1: Fetch the pinned minified libs** (keep their license header comments):

```bash
cd python/src/apx_agent/_static/vendor 2>/dev/null || mkdir -p python/src/apx_agent/_static/vendor && cd python/src/apx_agent/_static/vendor
curl -fsSL https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js -o marked.min.js
curl -fsSL https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js -o purify.min.js
```

- [ ] **Step 2: Verify they downloaded intact** (non-empty, look like the libs, expose globals):

```bash
wc -c marked.min.js purify.min.js           # both should be tens of KB
head -c 200 marked.min.js                    # expect a /*! marked ... MIT ... */ banner
head -c 200 purify.min.js                    # expect a /*! @license DOMPurify ... */ banner
grep -c "marked" marked.min.js               # >0
grep -c "DOMPurify" purify.min.js            # >0
```
Expected: both files are tens of KB, carry license banners, and reference `marked` / `DOMPurify`. If a download is empty or HTML (an error page), STOP — try the unpkg mirror (`https://unpkg.com/marked@12.0.2/marked.min.js`, `https://unpkg.com/dompurify@3.1.6/dist/purify.min.js`).

- [ ] **Step 3: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_static/vendor/marked.min.js python/src/apx_agent/_static/vendor/purify.min.js
git commit -m "chore(dev-ui): vendor marked + DOMPurify for chat markdown rendering"
```

---

### Task 2: Serve `_static/vendor/` via a dev-UI route

The topology assets route at `_dev.py:~756` is the pattern (`FileResponse` from a path under `_static/`, with traversal guarded). Add a sibling route for vendor JS.

**Files:**
- Modify: `python/src/apx_agent/_dev.py` (near the topology static routes, ~line 714-758)
- Test: `python/tests/test_dev_ui_routes.py`

- [ ] **Step 1: Write the failing test** — add to `tests/test_dev_ui_routes.py`:

```python
class TestVendorAssets:
    async def test_serves_marked(self, app):
        from httpx import ASGITransport, AsyncClient
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/marked.min.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert len(r.content) > 1000

    async def test_serves_purify(self, app):
        from httpx import ASGITransport, AsyncClient
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/purify.min.js")
        assert r.status_code == 200

    async def test_vendor_path_traversal_blocked(self, app):
        from httpx import ASGITransport, AsyncClient
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/../topology/index.html")
        assert r.status_code in (403, 404)
```

(Use the same `app` fixture the other route tests in this file use — inspect the top of `test_dev_ui_routes.py` for how `app` is built, and match it. If route tests there aren't async, mirror their exact style.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py -k Vendor -v`
Expected: FAIL (404 — route not registered).

- [ ] **Step 3: Add the route** — in `_dev.py`, next to the topology static routes, add (match the surrounding router/registration style — the topology routes use a `_TopoPath(__file__).parent / "_static"` base and `FileResponse`):

```python
    _vendor_root = _TopoPath(__file__).parent / "_static" / "vendor"

    @router.get("/_apx/vendor/{filename}", include_in_schema=False)
    async def vendor_asset(filename: str) -> Any:
        # Only serve known vendored files from the vendor dir; resolve and
        # confirm the result is inside _vendor_root (block path traversal).
        target = (_vendor_root / filename).resolve()
        if _vendor_root.resolve() not in target.parents or not target.is_file():
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        return FileResponse(target, media_type="application/javascript")
```

(Confirm the router variable name used by the nearby topology routes — it may be `router`, `app`, or an `APIRouter`; match it exactly. `FileResponse` is already imported at `_dev.py:26`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py -k Vendor -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_dev.py python/tests/test_dev_ui_routes.py
git commit -m "feat(dev-ui): serve vendored JS from /_apx/vendor/"
```

---

### Task 3: Render assistant markdown in the chat

**Files:**
- Modify: `python/src/apx_agent/_ui_chat.py` — `_render_agent_ui` (script tags + CSS), `addMsg` (~1180), the stream loop (~1454/1462/1477)
- Test: `python/tests/test_dev_ui_routes.py`

- [ ] **Step 1: Write the failing test** — add to `tests/test_dev_ui_routes.py` (reuse the `TestLandingRender._ctx` style or `_make_ctx`):

```python
class TestMarkdownWiring:
    def test_page_loads_vendor_libs_and_renders_assistant(self):
        from apx_agent._ui_chat import _render_agent_ui
        from tests.test_dev_ui_routes import _make_ctx  # or build a ctx inline
        html = _render_agent_ui(_make_ctx())
        assert "/_apx/vendor/marked.min.js" in html
        assert "/_apx/vendor/purify.min.js" in html
        assert "DOMPurify.sanitize(marked.parse(" in html      # render wiring present
        assert "renderAssistantInto" in html                   # helper present
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py -k MarkdownWiring -v`
Expected: FAIL.

- [ ] **Step 3: Add the script tags** — in `_render_agent_ui`, in the `<head>` (or just before the main `<script>` at ~line 779), add:

```html
<script src="/_apx/vendor/marked.min.js"></script>
<script src="/_apx/vendor/purify.min.js"></script>
```

- [ ] **Step 4: Add the render helper + use it in `addMsg`** — replace the `addMsg` body (~1180):

```javascript
function renderAssistantInto(el, text) {{
  // Sanitized markdown → HTML for assistant messages. marked parses, DOMPurify
  // strips anything unsafe (escape-by-default; the model never injects raw HTML).
  try {{
    el.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
  }} catch (e) {{
    el.textContent = text;  // never break the chat on a render error
  }}
}}
function addMsg(role, text, streaming) {{
  const div = document.createElement('div');
  div.className = `msg ${{role}}${{streaming ? ' streaming' : ''}}`;
  if (role === 'assistant') renderAssistantInto(div, text);
  else div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}}
```

- [ ] **Step 5: Update the streaming loop** — in the stream loop (~1454, 1462, 1477 and any other `assistantDiv.textContent = full;` in that handler), replace each `assistantDiv.textContent = full;` with:

```javascript
            renderAssistantInto(assistantDiv, full);
```

(Search the whole send-handler for `assistantDiv.textContent` and replace all occurrences. Leave the `user` message rendering as-is.)

- [ ] **Step 6: Add markdown CSS** — in the `<style>` block, scoped to `.msg.assistant`:

```css
  .msg.assistant table {{ border-collapse: collapse; margin: 8px 0; font-size: 12px; width: 100%; }}
  .msg.assistant th, .msg.assistant td {{ border: 1px solid #2a2a2a; padding: 5px 9px; text-align: left; }}
  .msg.assistant th {{ background: #161616; color: #cfe; font-weight: 600; }}
  .msg.assistant tr:nth-child(even) td {{ background: #0f0f0f; }}
  .msg.assistant pre {{ background: #111; border: 1px solid #222; border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 12px; }}
  .msg.assistant code {{ background: #15171a; border-radius: 4px; padding: 1px 5px; font-size: 12px; font-family: ui-monospace, monospace; }}
  .msg.assistant pre code {{ background: none; padding: 0; }}
  .msg.assistant h1, .msg.assistant h2, .msg.assistant h3 {{ margin: 10px 0 6px; line-height: 1.3; }}
  .msg.assistant ul, .msg.assistant ol {{ margin: 6px 0 6px 20px; }}
  .msg.assistant li {{ margin: 2px 0; }}
  .msg.assistant a {{ color: #60b0ff; }}
  .msg.assistant p {{ margin: 6px 0; }}
  .msg.assistant > :first-child {{ margin-top: 0; }}
  .msg.assistant > :last-child {{ margin-bottom: 0; }}
```

(Remember `_ui_chat.py` is f-string-heavy — double every literal `{` / `}` in the CSS as shown.)

- [ ] **Step 7: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py -k "MarkdownWiring or Landing" -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add python/src/apx_agent/_ui_chat.py python/tests/test_dev_ui_routes.py
git commit -m "feat(dev-ui): render assistant chat messages as sanitized markdown"
```

---

### Task 4: Full-suite + pyright gate

- [ ] **Step 1:** `cd python && git checkout -- uv.lock 2>/dev/null || true`
- [ ] **Step 2:** `cd python && uv run pytest -q` — expect all pass (baseline ~1830 + new tests, 1 skipped).
- [ ] **Step 3:** `cd python && uv run pyright src/apx_agent/_dev.py src/apx_agent/_ui_chat.py` — expect 0 errors.
- [ ] **Step 4:** `cd python && git checkout -- uv.lock 2>/dev/null || true && git status --short` — only intended files modified; uv.lock clean; the two vendored `.js` files present.

---

## Self-review

**Spec coverage:** vendor libs (Task 1 ✓), local serving no-CDN (Task 2 ✓), assistant markdown render + user-plain (Task 3 ✓), table CSS (Task 3 step 6 ✓), streaming re-render (Task 3 step 5 ✓), tests (Tasks 2-3 ✓). Out-of-scope items untouched.

**Placeholder scan:** none — concrete code/commands throughout. The two CDN URLs are pinned (marked@12.0.2, dompurify@3.1.6) with a stated fallback mirror.

**Type/name consistency:** `renderAssistantInto(el, text)` defined in Task 3 step 4 and used in step 5; `/_apx/vendor/{filename}` route (Task 2) matches the `<script src="/_apx/vendor/...">` tags + the wiring test (Task 3). `_vendor_root` defined once.

**Verify-on-execute:** confirm the nearby topology routes' router variable + `_TopoPath` helper names in `_dev.py`; confirm the `app` fixture style in `test_dev_ui_routes.py`; find ALL `assistantDiv.textContent` occurrences in the send handler (not just the 3 cited lines — line numbers may drift).
