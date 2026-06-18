# Dev-UI Hardening — Design (Pilot: memory + conversations)

> Status: DRAFT for plan-gate sign-off. One open fork (see §"Open fork").
> Scope grounded in read-only recon (codex, file:line evidence inline).

## Goal & scope
Harden the agent `_apx` dev UI for reliability. **Hardening only — no new features.**
Surface is the **agent `_apx` dev UI only**; the hub (`/api/*` registry) is explicitly
out of scope (its zero-test gap is a separate side project).

## Non-goals
- No new features. "Prompt registry" reduces to hardening prompt-assembly's surface
  (deferred unless it appears in a fan-out group); we are NOT building a registry.
- Hub zero-test gap — separate side project.
- A2A task lifecycle — separate, still-open decision.

## Locked decisions
1. Hardening only.
2. `_apx` dev UI only.
3. Pilot (memory + conversations) → then fan out the proven pattern.
4. Typed contracts — **strict request validation where request bodies exist** (see recon adj. #1).
5. Single honest OpenAPI source of truth.
6. `caps` read-after-write manifest **from day one** + extended reality tests.
7. Same-PR UI conformance (where the UI must change).

## Recon-driven adjustments (file:line grounded)
- **#1 — Pilot routes are GET-only, no request bodies.**
  `GET /_apx/conversations` (`python/src/apx_agent/_dev.py:1005`),
  `GET /_apx/conversations/{conv_id}/items` (`_dev.py:1038`),
  `GET /_apx/memories` (`_dev.py:1917`) — all raw `Request`→`JSONResponse`, no body read.
  ⇒ Strict *request* validation has nothing to bite on in the pilot. Pilot contract
  work = **response models + honest OpenAPI**. Strict request models get exercised at
  fan-out where POST surfaces (eval/setup/approval) live.
- **#2 — `/_apx/openapi.json` has no UI consumer.** Served at `_dev.py:969` from a
  hand-built tool-only spec via `_build_apx_openapi_spec` (`_ui_chat.py:2572`,
  docstring `:2577-2580`); no frontend fetch anywhere. ⇒ Retiring/repointing is
  zero-UI-risk.
- **#3 — The "React UI" is embedded JS generated from `_ui_chat.py`**, not a separate
  React/TSX app. Pilot UI call sites: `_ui_chat.py:1301` (conversations list),
  `:1385` (items, promote-to-eval), `:1410` (items, switch conversation),
  `:1664` (memories preview) — all GET reads. ⇒ UI conformance only matters if we
  change response shapes (we won't). Pillar 4 near-empty for the pilot.
- **#4 — Read-after-write coverage is asymmetric.** Conversations covered by
  `python/tests/test_dev_ui_reality_ctk.py` (list `:94`, items `:95`, isolation `:126`).
  `/_apx/memories` route has **no** coverage; only the memory *tool* is covered
  (`test_memory_tool_reality_ctk.py:39-44`). ⇒ Pilot adds memory-route reality coverage.

## Pillars (re-scoped for the pilot)
1. **Response contracts.** Add Pydantic `response_model`s to the 3 pilot routes and
   un-hide them into native FastAPI OpenAPI (`include_in_schema=True`). Model the
   *existing* response shapes so embedded-JS reads stay valid:
   - conversations: `id`, `title`, `created_at`, `updated_at` (`_dev.py:1021`)
   - items: `id`, `type`, `data` (`_dev.py:1047`)
   - memories: `id`, `content`, `namespace`, `updated_at` (`_dev.py:1937`)
   Strict *request* models: N/A for pilot GETs — pattern proven at fan-out.
2. **OpenAPI single source of truth.** Pilot routes enter the native schema. Repoint
   `/_apx/openapi.json` to `request.app.openapi()` or delete it; if the tool-only spec
   is still needed for Scalar/LLM tooling, expose it under a clearly-named *separate*
   endpoint instead of overloading `/_apx/openapi.json`. No UI fetch to update.
3. **caps + read-after-write.** New `capabilities.yaml` declaring:
   - "conversation list & items round-trip via `/_apx` routes" (backed by existing reality tests)
   - "a stored memory persists and reads back via `GET /_apx/memories`" (NEW check)
   Each backed by a real read-after-write check; `python -m caps verify` green & fresh.
   Add the missing memory-route reality test; reuse existing conversation reality tests.
4. **UI conformance.** Verify the embedded-JS reads still match the typed response
   shapes (no change expected since we model existing shapes). Only edit `_ui_chat.py`
   if a response field is renamed.

## Open fork (needs sign-off)
The pilot surfaces are **read-only**, so the pilot cannot exercise the strict
*request*-validation half of the pattern. Two options:
- **(A) Keep pilot read-only [RECOMMENDED]** — prove response-models + OpenAPI +
  read-after-write here; prove strict request validation at fan-out where POSTs exist.
- **(B) Add one write surface** (e.g. eval-create or a setup POST) to the pilot to
  prove the full pattern including request validation now — at the cost of widening the
  pilot beyond memory + conversations.

## Pilot PR (single cohesive PR — the proving template)
- response models + un-hide routes + OpenAPI convergence
- `capabilities.yaml` + caps wiring
- memory-route reality test + reuse conversation reality tests
- Gates: pytest reality tests green; `python -m caps verify` green & fresh;
  pyright 0 new errors; scope limited to the intended files; no attribution trailers.

## Routing (cross-vendor)
- Implement: **claude_code** (multi-file + test-writing). Review: **codex** (different vendor).
- Fallback if claude_code boot-fails: codex implements → **pi** reviews (preserve cross-vendor).
- Worktree off `origin/main`, under `.worktrees/<id>`. polly never merges; human merges.

## Fan-out (after pilot approved & merged)
Apply the proven template across remaining `_apx` route-groups (schema, codegen, setup,
eval, trace/replay/approval) in parallel — one PR per group, each cross-reviewed;
introduce **strict request models where POST bodies exist**; extend `capabilities.yaml`;
converge any remaining hand-built OpenAPI.
