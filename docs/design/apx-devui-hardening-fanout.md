# Dev-UI Hardening — Fan-Out Plan

> Status: DRAFT for plan-gate sign-off. Follows the pilot (PR #213).
> Grounded in read-only recon of all 57 `_apx` route decorators in
> `python/src/apx_agent/_dev.py` (codex write-surfaces + claude_code read/action).

## Template established by the pilot (#213)
Per route-group PR: typed Pydantic **response_model**s on JSON routes → un-hide
into the **native** FastAPI OpenAPI (`include_in_schema=True`) → genuine
read-after-write reality tests in `test_dev_ui_reality_ctk.py`. caps stays
LOCAL (excluded). The fan-out adds the net-new leg: **strict request models**
where request bodies exist + **same-PR UI conformance**.

## Complete route accounting (57 decorators)
- **Pilot, done (3):** memories, conversations, conversation items.
- **Out of scope (8 orphans):** `/`, `/_apx/agent`, `/_apx/chat`, `/_apx/tools`
  (redirect), `/_apx/probe`, `/_apx/topology` (html), `/_apx/topology/assets`,
  `/_apx/vendor` (already hardened). HTML shells / static / redirects — no typed
  contract.
- **Out of scope, KEEP HIDDEN (privileged, 1):** `/_apx/deploy/stream` — SSE that
  spawns `apx deploy` subprocess; token-gated; not response_model-able. Do NOT
  un-hide.
- **To harden (45):** sliced into PRs below.

## Cross-cutting POLICY decisions (decide at plan gate — they shape every write PR)
1. **Error-status convention.** Strict request models auto-**422** on
   malformed/missing-required, replacing several handlers' current typed **400**
   (e.g. eval/judge, replay, setup). RECOMMEND: adopt 422 for shape/required
   validation (update the asserting tests), keep semantic **404/503** in the
   handler (tool-not-found, no-agent-context). Consistent with "strict from the
   start."
2. **OpenAPI exposure — RESOLVED by recon (codex explore-fanout-exposure-and-models).**
   Facts: (a) the generated **production** Databricks Apps path (`AgentServer`)
   mounts the dev UI router ONLY when `DATABRICKS_APP_PORT` is ABSENT — so real
   prod does NOT mount these routes at all (_wiring.py:835/839/843, cli.py:1019);
   (b) where the dev UI IS mounted (`create_app`/local/dev), native `/openapi.json`
   is reachable by any AUTHENTICATED Databricks App user with app access — NOT the
   anonymous public (Apps require auth); (c) the write POSTs stay token-gated at
   EXECUTION (403 without APX_DEV_UI_TOKEN) regardless of being documented. So
   un-hiding documents the write-route SHAPE to an audience that already has app
   access (and can't invoke without the token), and documents NOTHING in real
   prod. NEW RECOMMENDATION: **un-hide everything (full honest swagger — the
   user's actual goal)**; `deploy/stream` stays out only because SSE isn't
   response_model-able. Optional belt-and-suspenders: keep `replay/*` hidden as
   the single most-privileged pair (OBO tool execution) — a minor sub-choice, not
   a security necessity.
3. **Request-model permissiveness.** Several routes get MORE fields from the UI
   than the handler reads (wizard/generate-tools: UI sends
   `table/catalog/schema/warehouse_id`, handler reads only `description`;
   compose `nodes` carry extra metadata; eval/data = flexible case dicts).
   RECOMMEND: model required fields strictly, `extra="ignore"` (or model the
   extras) so strict validation never 422s real UI traffic. Per-route.
4. **UI error-surfacing.** Some write flows ignore the response, so a 422 would
   be SILENT (eval/data save, setup create-tool/generate-tools render 200
   `ok:false` as success — `_ui_setup.py:555,1549`; `_ui_chat.py:524,1492`).
   Same-PR UI conformance now includes surfacing these errors.
5. **Sequencing.** RECOMMEND phased, not all-at-once: Wave 1 (reads) now →
   Wave 2 (writes) after policy is locked and Wave 1 proves the read-extension →
   Wave 3 (replay) last.

## PR slices

### Wave 1 — Reads (low risk: response_model + un-hide + reality tests; NO request models)
- **PR-R1 — setup discovery + schema.** GET catalogs/schemas/tables/warehouses/
  agents/tools/vs-indexes/agent-pattern/`tools/schema` (+ probe-json, token-gated
  side-effecting GET — careful). Many have 500-on-SDK-error `{error}` shapes to
  model. Biggest *test* gap: `tools/schema` has zero HTTP tests.
- **PR-R2 — trace + approval.** traces (GET×2; `fmt` HTML/JSON duality — model
  the JSON branch; detail returns `{spans}` OR `{error}` union), approvals list +
  approve/deny (POST but **no body** — response_models only, NO request model).
  Mostly already reality-tested.
- **PR-R3 — orphan JSON reads.** `openapi.json` (curated tool spec, distinct from
  native), `probe/checks` (well-tested), `topology.json`, `topology/inspect`
  (404 via HTTPException), `workspace-context`. response_models + un-hide +
  reality tests where missing.

### Wave 2 — Writes (strict request models per locked policy; same-PR UI conformance)
- **PR-W1 — eval.** GET/POST `/eval/data` (permissive list-of-flexible-case
  model — UI shapes diverge: `expected_judge` vs `expected`, optional
  `criterion`, run metadata), POST `/eval/judge` (`{question, response,
  criterion, model?}`), GET `/eval`. Fix silent-save (UI ignores response). Add
  reality tests.
- **PR-W2 — codegen / edit.** edit GET/POST, edit/preview, tools/suggest,
  tools/new (permissive nested `params`), tools/{fn} DELETE, wizard/generate-tools
  (**extra-fields trap**), setup/create-tool, setup/wire-agent. Source-mutating →
  type-but-hidden. Add tool-splice read-after-write reality test. Surface errors.
- **PR-W3 — setup writes / composition.** setup POST (save), generate-instructions
  (fix exception-bubbling), apply-instructions, agents POST, agent-pattern POST,
  compose (`nodes` extra fields). Source-mutating → type-but-hidden.

### Wave 3 — Privileged
- **PR-P1 — replay.** replay/tool (`{tool_name, args:dict}`), replay/llm
  (`{messages:list, model?}`). NO UI wiring — contract is the test suite; keep
  `args`/`messages` PERMISSIVE. Decide 422-vs-asserted-400 (update tests).
  **Type but KEEP HIDDEN** — executes arbitrary registered tools with forwarded
  OBO creds + invokes the LLM; do not advertise in Scalar. Own PR.

## Routing & mechanics
- **STRUCTURE (anti-conflict, baked into every brief):** all new Pydantic
  request/response classes live in a NEW shared sibling module
  `python/src/apx_agent/_apx_models.py` (recon-confirmed feasible: low circular
  risk — _wiring.py imports _dev lazily; matches the package convention of
  _models.py/_conversation.py). Each route-group PR then touches only (i) its own
  model classes in _apx_models.py and (ii) its own disjoint decorator range in
  _dev.py (`response_model=...` + `include_in_schema=True`). The ONLY shared
  hotspot is the import block near _dev.py:18 (one added import line per PR —
  trivial to reconcile). This is the #207↔#210 merge-conflict lesson applied.
- Each PR = its own worktree off current `origin/main`, its own implementer, its
  own PR, cross-reviewed by the OPPOSITE vendor. polly never merges.
- Within a wave, PRs are independent → run in parallel (fanout). Reads touch
  disjoint handler regions; low conflict risk, but stagger if two PRs edit the
  same file region.
- Gates per PR (orchestrator-verified before review): pytest (changed-surface +
  reality tests) green; pyright 0 new errors; scope clean; caps local-only;
  NO attribution trailers.
- caps: each write PR MAY add local capabilities for its read-after-write checks
  (local-only, never pushed) — optional, since the tracked reality tests carry
  the shipped proof.

## Latent bugs surfaced (fold into the relevant write PR or file as follow-ups)
- Some setup flows render 200 `ok:false` as success (no `d.ok` check) —
  `_ui_setup.py:555,1549`.
- `setup/generate-instructions` bubbles generator exceptions server-side instead
  of a graceful error JSON (`_dev.py:2068`).
- PR #208's `test_dev_ui_route_coverage.py` appears NOT to be on current main —
  confirm whether that coverage exists before assuming it.

## Open question for the user (build-vs-defer, separate)
This plan is hardening only. The A2A `/tasks/*` build decision and Thread B
(Python DSL vs YAML) remain separate, parked.
