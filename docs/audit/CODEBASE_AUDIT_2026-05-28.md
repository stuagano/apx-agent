# apx-agent Codebase Audit — 2026-05-28

## Executive Summary

This audit consolidates the confirmed findings of 22 subsystem finders across the apx-agent codebase — a dual-language (Python + TypeScript) SDK for building governed Databricks Apps agents. Every finding below was adversarially re-verified against the source by a skeptical second agent. Of 99 raw confirmed findings, one true duplicate was merged, leaving **97 distinct findings: 2 critical, 21 high, 34 medium, and 40 low**. The high-and-low-severity items (`verified: true`) were checked line-by-line; the low-severity items (`verified: null`) are honest pass-throughs that were not independently re-verified and should be treated as plausible-but-unconfirmed.

The overall health picture is that of a feature-rich, fast-moving SDK with **solid intent but inconsistent execution of its own safety patterns**. The codebase repeatedly demonstrates that it *knows* the right thing to do — it has bind-parameter helpers, an `esc()` HTML escaper, an `include_spans=False` blob-storage workaround, a `_validate_table_name` allowlist, a `_same_host` OBO guard — and then fails to apply those patterns uniformly. The dominant theme is **the safe pattern existing in one place and the unsafe variant surviving a few lines or one module away.** This shows up as supply-chain corruption, SQL-injection sinks, credential exfiltration, silent error-swallowing on governed-data paths, and Python/TypeScript behavioral drift.

The two critical findings are both single-point catastrophic: a Makefile `sed` that clobbers *every* dependency hash in a generated lockfile (guaranteed-broken deploy on a documented path), and a hub frontend that relays a user's raw Databricks OBO token to any registrant-supplied agent URL (direct credential exfiltration with no auth gate on registration). The high-severity tier is dominated by two clusters: **silent failure on governed-data reads** (vector search, memory recall, session-store, canary/trace analysis all swallow backend errors and return empty/clean results that mislead the LLM or greenlight bad rollouts) and **SQL string-literal escaping that omits backslashes** (five separate Delta/Spark write paths, several reachable from untrusted request input). The dev UI (`/_apx/*`) is a recurring security surface — it is mounted unconditionally with no auth gate and exposes a code-write/RCE-equivalent endpoint and an unguarded SSRF probe.

A notable property of this dataset: **verification narrowed or corrected a large fraction of the confirmed findings.** Many finder line numbers were stale (the audit below uses the verifier-corrected lines), several impact claims were overstated (e.g. an "uncaught traceback crash" that is in fact caught and misreported; "tool names injected verbatim" when names are actually slugified; "one leading `../` escapes" when it does not), and at least one cited docstring quote was fabricated. Where a finding's evidence is thinner than its headline, this report says so explicitly.

---

## Findings by Severity

> Tag legend: `[verified]` = confirmed against source by the skeptical reviewer; `[UNVERIFIED]` = low-severity pass-through, not independently re-verified. Confidence is the finder's stated confidence.

---

### Critical

#### C1. Makefile global `sed` clobbers every dependency's integrity hash in `.build/uv.lock`
- **File:** `Makefile:7-9` (the unscoped `sed -i '' "s/sha256:[a-f0-9]\{64\}/sha256:$HASH/g"`)
- **Category:** security / supply-chain
- **What's wrong:** The wheel-build target patches the locally-built `apx_agent` wheel hash into `.build/uv.lock` using a *global*, unscoped substitution. A `uv.lock` records a `sha256` for every package artifact, so the global pattern overwrites all of them with the apx wheel hash.
- **Evidence:** Empirically confirmed — source `python/uv.lock` has 3353 unique sha256 values; the Makefile-produced `python/hello-world/.build/uv.lock` collapsed to exactly 1 (the apx wheel hash `d569b1dc…adf075`), now stamped onto `aiohappyeyeballs`, `aiohttp`, and every other package. `uv` verifies recorded artifact hashes, so `uv sync` against this lock fails verification → broken `bundle deploy`. Only line 205 (the `apx_agent-*.whl` path source) legitimately needs patching.
- **Why it's not also on the main path:** `apx deploy` regenerates `.build/uv.lock` via `uv lock` (cli.py:~2247) and never uses the Makefile `sed`, so it is unaffected. `.build/uv.lock` is untracked, which is why CI never caught it. The blast radius is the documented `make wheel → bundle deploy` flow.
- **Recommendation:** Address-restrict the `sed` to the apx wheel line only (`-E "/filename = \"apx_agent-.*\\.whl\"/ s/…/"`, no `g` flag), or drop hash patching entirely and run `uv lock` as `apx deploy` already does. Add a guard asserting the distinct-sha256 count is unchanged after patching.
- **Tag:** `[verified]` — confidence **high**

#### C2. Hub frontend forwards the user's Databricks OBO token to any registrant-controllable agent URL
- **File:** `hub/src/agent_hub/backend/router.py:264-288` (driven by `ChatPanel.tsx:42`, `routes/agents/$agentId.tsx:51`); `models.py:30`; `app.py:39-40`
- **Category:** security
- **What's wrong:** `invoke_agent` reads the caller's `X-Forwarded-Access-Token` (the user's Databricks on-behalf-of OAuth token) and forwards it verbatim as `Authorization: Bearer` to `{agent.url}/responses`. `agent.url` comes from `POST /api/agents/register`, whose `RegisterRequest.url` is an unvalidated plain `str`; `register_agent` has no auth dependency and the router is mounted with no auth middleware. So any authenticated app user can register an agent pointing at an attacker host serving a valid `/.well-known/agent.json`, and a subsequent invoke ships the user's full OBO token to that host — credential exfiltration plus SSRF.
- **Evidence:** Confirmed end-to-end. Note one corrected overstatement: the *UI* Send button is gated on `agent.supports_invoke` (which registrant-added agents default to `False`), so the button would not appear — but the `/invoke` endpoint itself has no such gate, so the vulnerability is fully exploitable via direct API call. The "frontend Send is the trigger" framing is therefore slightly inaccurate; the underlying credential exfiltration is real and unmitigated.
- **Recommendation:** Do not relay the user's token to arbitrary registrant-supplied hosts. Restrict invoke targets to a trusted host-suffix allowlist (workspace host / `*.databricksapps.com`), validate `RegisterRequest.url` as an `HttpUrl` against that allowlist, gate `/api/agents/register` behind authorization, and prefer minting a scoped downstream token over relaying the raw OBO token.
- **Tag:** `[verified]` — confidence **high**

---

### High

#### H1. `run_via_compile` / `stream_via_compile` block the event loop with synchronous `graph.invoke`/`stream`
- **File:** `python/src/apx_agent/_compile_run.py:181, 208`
- **Category:** performance
- **What's wrong:** Both `async def` entry points call LangGraph's synchronous APIs (`compiled.invoke(...)`, `for chunk in compiled.stream(...)`) directly on the asyncio event-loop thread with no `await`/`run_in_executor`. The entire multi-step LLM + tool-dispatch loop runs synchronously; under concurrency on the FastAPI Apps runtime, one in-flight agent run stalls every other coroutine. The authors already offload *tool* execution off a running loop (`_compile.py:178-186`), so the awareness exists, but the top-level invoke/stream still blocks. `BaseAgent.run/stream` await these, so it propagates to the `/invocations` and chat handlers.
- **Recommendation:** Offload via `anyio.to_thread.run_sync` / `loop.run_in_executor`; use `astream`/`ainvoke` for the async path.
- **Tag:** `[verified]` — confidence **high** (magnitude depends on deployment concurrency settings)

#### H2. Responses-agent message conversion drops assistant `tool_calls`, orphaning tool results
- **File:** `python/src/apx_agent/_responses_agent.py:210-211, 369-376`
- **Category:** correctness
- **What's wrong:** Both Responses-path converters rebuild assistant messages as bare `AIMessage(content=content)` with no `tool_calls=`, then build a following `ToolMessage` whose `tool_call_id` references a call that exists on no `AIMessage` — the exact "orphan tool_result" shape that Databricks-Claude rejects. The ChatAgent path preserves `tool_calls` correctly, so this is parity drift in the Responses runtime only.
- **Evidence:** The history-replay path is the guaranteed trigger: persisted history carries explicit `role` values, and `_history_to_langchain` drops the `tool_calls` key. (Correction: in the *input* path, echoed `function_call` items have no `role`, so they fall into the `HumanMessage` branch — still broken, but via a different mechanism than the finder described.)
- **Recommendation:** Reconstruct `tool_calls` onto the `AIMessage` (mirror `_chat_agent._to_langchain_messages`) and map `function_call` input items to assistant `tool_calls`.
- **Tag:** `[verified]` — confidence **high**

#### H3. Async `before_*` hooks are fire-and-forget, breaking the documented "raising aborts the call" contract
- **File:** `python/src/apx_agent/_callbacks.py:62-72`
- **Category:** correctness / security-guardrail
- **What's wrong:** The docstrings promise that raising in a `before_*` hook aborts the tool/model call. But under a running loop (the normal uvicorn case), `_run_hook` schedules an async hook with `loop.create_task(hook(...))` and returns immediately. The LangChain `on_tool_start`/`on_chat_model_start` callbacks are synchronous, so the detached task's exception fires later ("Task exception was never retrieved") and can never propagate to abort the call. A guardrail/security hook written as a coroutine silently fails to block execution. Only sync hooks honor the contract.
- **Recommendation:** Either document that only synchronous `before_*` hooks can abort and run async hooks through a runner that surfaces exceptions, or drive hooks via LangChain's async callback methods where awaiting is possible.
- **Tag:** `[verified]` — confidence **high**

#### H4. Public `build_tool` docstring teaches SQL injection via f-string interpolation of LLM input
- **File:** `python/src/apx_agent/_tool_factory.py:13-21`
- **Category:** security
- **What's wrong:** `build_tool` is the documented public entry point for custom governed tools, and its canonical docstring example does `run_sql(ws, f"SELECT * FROM {table} WHERE id = '{id}'")`, interpolating the LLM-controlled runtime argument `id` directly into SQL. `run_sql` executes under the caller's UC identity, and `_sql.py`'s own docstring explicitly warns against this exact pattern — so the flagship copy-paste template contradicts the framework's documented safe path and will propagate the footgun into every hand-written tool.
- **Evidence:** This is a documentation/example defect (the docstring is never executed), which slightly tempers severity, but it is the primary teaching surface for the public tool-authoring API.
- **Recommendation:** Rewrite the example to use bind parameters (mirror `_sql.py`), and note that identifiers like `table` cannot be bound and must come from a trusted allowlist.
- **Tag:** `[verified]` — confidence **high**

#### H5. `RemoteDatabricksAgent` forwards user OBO token to an attacker-controllable URL from the fetched agent card
- **File:** `python/src/apx_agent/_remote.py:161-186, 292-347`
- **Category:** security
- **What's wrong:** `_fetch_card` unconditionally rewrites the outbound base URL with the `url` field from the fetched card body (`self._base_url = self._card.url.rstrip("/")`), with no allowlist/origin check. `_obo_headers` then copies the caller's `Authorization` / `X-Forwarded-Access-Token` into the outbound headers sent to `{base_url}/responses`. A malicious card can redirect the user's OBO token to an arbitrary host.
- **Evidence (why high, not critical):** Two mitigations the verifier confirmed. (1) For a normal `databricksapps` card, `_app_name` is set at construction and not recomputed, so `run()` takes the SDK gateway path and the credential-forwarding HTTP fallback fires *only* if the SDK call raises — fallback-only, not unconditional. The clean always-leak path requires a non-databricksapps custom host. (2) The call sites of `from_card_url`/`from_app_name` are operator/developer-configured; no path feeding *attacker-controlled* data into `from_card_url` was demonstrated. Distinct from C2 (hub): there the registrant is any unauth app user; here the source is operator-controlled.
- **Recommendation:** Pin `base_url` to the operator-supplied card_url origin (scheme+host) and reject mismatched `card.url`, or enforce a trusted-host allowlist before forwarding any credential header.
- **Tag:** `[verified]` — confidence **high**

#### H6. Delta MERGE string escaping doubles single quotes but not backslashes (write loss on routine content)
- **File:** `python/src/apx_agent/_memory_delta.py:86-93` (`_quote_string`, used via `_merge_sql`)
- **Category:** correctness / security
- **What's wrong:** `_quote_string` only doubles single quotes; Spark SQL processes backslash as an escape character by default. Memory content is arbitrary LLM/tool output flowing from the `remember` tool. Content ending in a backslash (Windows paths, regex, JSON, LaTeX) renders as `'…\'` — the backslash escapes the closing quote → unterminated literal → MERGE parse failure → write loss. Embedded `\'` sequences corrupt the literal and open an injection vector. The Lakebase sibling uses proper bind parameters, confirming a safe path existed and was not used.
- **Shared root:** This same `_quote_string` helper backs the `_example_delta.py` write path (see H7's scope), so a single fix resolves both. Scope is broader than memory alone.
- **Recommendation:** Escape backslashes before single quotes, or use parameterized SQL. Apply to `_sql_array_str`, the metadata literal, and `_example_delta.py`.
- **Tag:** `[verified]` — confidence **high**

#### H7. Delta string-literal escaping does not handle backslashes on user-controlled session & example text
- **File:** `python/src/apx_agent/_session_delta.py:44-46` (`_sql_str`); `_example_delta.py:189-197` (via shared `_quote_string`)
- **Category:** security
- **What's wrong:** Same backslash-escaping gap as H6, but here on directly user-controlled input: `session_id` flows from `custom_inputs.get("session_id")` into SELECT/DELETE/MERGE, and example mining writes raw user/assistant conversation text verbatim. A `\'`-bearing or backslash-terminated value breaks out of the literal — genuine breakout, not merely malformed SQL. Lakebase siblings use bind parameters, so the data is safe on Lakebase and unsafe on Delta.
- **Evidence:** `session_id` is fully attacker-controlled. Not raised to critical only because it is Delta-backend-conditional and warehouse execution privilege is uncertain.
- **Recommendation:** Escape backslashes as well, or route Delta writes through parameterized statements. Add a backslash-terminated `session_id`/example regression test.
- **Tag:** `[verified]` — confidence **high**

#### H8. SQL injection in `export_traces` MERGE — request-supplied `session_id` reaches inline VALUES; `_escape_sql` omits backslashes
- **File:** `python/src/apx_agent/_trace_export.py:69-72, 253-291`
- **Category:** security
- **What's wrong:** `export_traces` builds the entire MERGE as an inline string; every value goes through `_escape_sql` (single-quote-doubling only, no backslash escape). `session_id` originates from caller-controlled `custom_inputs`, is stamped as the `apx.session.id` span attribute, and is pulled straight into the VALUES tuple. A backslash-bearing value breaks out of the literal → arbitrary SQL on the warehouse. The module already imports `run_sql`, which supports safe bind parameters, but `export_traces` bypasses it.
- **Evidence:** This is a *stored / second-order* injection — it fires when the batch exporter runs, not synchronously with the attacker request. The asynchronous trigger is why it is high, not critical.
- **Recommendation:** Use `run_sql`'s parameterized bind path with a parameterized MERGE (the safe API is already in this module), or at minimum extend `_escape_sql` to escape backslashes.
- **Tag:** `[verified]` — confidence **high**

#### H9. Trace-read calls silently degrade on blocked blob storage (FEVM/private-link); canary can greenlight a bad rollout, and the probe reports OK
- **File:** `python/src/apx_agent/_canary.py:396-402` (and `_canary_apps.py:342`, `_eval_chain.py:142`, `_trace_export.py:223`)
- **Category:** error-handling
- **What's wrong:** The codebase explicitly knows the FEVM footgun — `_dev.py:552-573` uses `search_traces(..., include_spans=False)` with a comment that it works when blob storage is blocked. But four sibling call sites do *not* pass `include_spans=False` and swallow the resulting failure into empty results: `_canary`/`_canary_apps` return an empty (clean-looking) report, `_eval_chain` returns `[]`. An empty canary report reads as "no errors observed" and can greenlight promoting a bad model. Worse, the existing `_ExportErrorCapture` mitigation listens on the *export/write* logger, while these are *read*-path failures, so `/_apx/probe` reports status `ok` during silent degradation — false confidence.
- **Recommendation:** Pass `include_spans=False` on every metadata-only `search_traces`; distinguish "blob blocked" from "no traces"; extend the probe to detect read-path failures.
- **Tag:** `[verified]` — confidence **high**

#### H10. SQL identifier injection in TypeScript Lakebase tools
- **File:** `typescript/src/connectors/lakebase.ts:126-135, 162-202`; `types.ts:271`
- **Category:** security
- **What's wrong:** The Lakebase tools parameterize *values* but interpolate *identifiers* raw: `table` (z.string(), no validation) into the FQN/SELECT, `columns` into the projection, INSERT column names from `Object.keys(values)`, and crucially `buildSqlParams` builds `${key} = :${key}` where the left-hand key is the attacker/LLM-controllable map key. Reachable two ways: LLM-generated tool args (steerable via prompt injection in retrieved docs) and the raw `POST /api/agent/tools/lakebase_query` body. The Statement Execution API only parameterizes value placeholders, not identifiers.
- **Evidence:** `catalog`/`schema` are fixed from config (not attacker-controlled), bounding blast radius to the resolved token's grants — but the injection is real and exploitable.
- **Recommendation:** Validate every interpolated identifier against a strict regex / known-schema allowlist at one chokepoint (including `buildSqlParams` keys), then backtick-quote.
- **Tag:** `[verified]` — confidence **high**

#### H11. `voynich-export` always exports zero candidates (`generation=-1` used as a literal filter)
- **File:** `python/src/apx_agent/workflow/cli.py:302`
- **Category:** correctness
- **What's wrong:** The export entrypoint calls `load_pareto_survivors(generation=-1, …)`, which emits `WHERE generation = -1`. Generations are always non-negative, and `-1` is never translated to "latest," so the predicate never matches; the entrypoint always writes `n_candidates=0` while printing a success message. The results-preservation checkpoint silently produces nothing.
- **Recommendation:** Resolve `MAX(generation)` when `generation < 0` (mirror `cli.py:168-171`), or make `load_pareto_survivors` treat negative generation as max.
- **Tag:** `[verified]` — confidence **high**

#### H12. `fitness_perplexity` is never populated, capping composite fitness at 0.75 and making the 0.85 escalation gate unreachable
- **File:** `python/src/apx_agent/workflow/loop_agent.py:95-102, 200, 353-357, 463-476`
- **Category:** correctness
- **What's wrong:** `composite_fitness()` weights five signals summing to 1.00, with perplexity at 0.25. `fitness_perplexity` is never assigned anywhere in the loop (statistical is computed locally; semantic/consistency/adversarial come from sub-agents; perplexity has neither). With perplexity pinned at 0, max composite = 0.75 < the default `escalation_threshold` 0.85, so the human-review escalation list can never be non-empty and `total_escalated` is always 0 under shipped defaults. (The threshold is env-configurable and `force_escalate` exists, but the *automatic* gate is dead.)
- **Recommendation:** Populate `fitness_perplexity` (local proxy or sub-agent), or renormalize weights to exclude it until wired; recompute the threshold so the gate can fire.
- **Tag:** `[verified]` — confidence **high**

#### H13. Chat-agent streaming drops session load/prepend/persist in Python but TS does all three (parity drift)
- **File:** `python/src/apx_agent/_chat_agent.py:361-408`
- **Category:** parity-drift
- **What's wrong:** With a `session_store` wired, Python's `predict_stream` does NOT load the session, prepend history, or persist the turn — it builds graph input directly from incoming messages. The TS `predictStream` does all three, and Python's own non-streaming `predict` (and both responses-agent streaming paths) handle sessions fully — so this is an oversight, not intent. Streaming a multi-turn conversation in Python forgets prior turns and saves nothing. Undocumented (the TS "known gaps" header covers only streaming shape, not session handling).
- **Recommendation:** Add session load/prepend/persist to Python's `predict_stream` to match `predict` and the TS port.
- **Tag:** `[verified]` — confidence **high**

#### H14. Delta-backed durable workflow engine (`DeltaEngine`) has zero test coverage
- **File:** `python/src/apx_agent/workflow/engine_delta.py:75-373`
- **Category:** test-gap
- **What's wrong:** `DeltaEngine` (374 LOC) is the durable persistence backend whose entire reason to exist is step idempotency/replay (a completed step must return stored output without re-invoking; a failed step must re-raise `StepFailedError`). No test references it; `test_workflow_engine.py` only exercises `InMemoryEngine`. The Delta paths (MERGE upsert, separate runs/steps tables, in-process `_step_cache` over the SQL lookup) are untested. Compounding: `step()` → `_lookup_step`/`_persist_step` interpolate `run_id`/`step_key` via `_esc` only, with no `_safe_name` guard (unlike `start_run`/`finish_run`), so the sole injection barrier and that validation asymmetry are also unverified.
- **Evidence:** Latent rather than actively breaking — `DeltaEngine` is exported/documented but not on a currently-live execution path in `src`. The gap is on a durability-critical component whose worst failure mode is silent re-execution/loss of committed work.
- **Recommendation:** Add a `DeltaEngine` test (fake WorkspaceClient) asserting cached-completed-returns-stored-output-without-rehandler, failed-step-re-raises, idempotent run reopen, and quote/backslash round-trip through `_esc`.
- **Tag:** `[verified]` — confidence **high**

#### H15. Wizard `loadTables()` treats an object response as an array — Explore/Tools steps always show "No tables found"
- **File:** `python/src/apx_agent/_ui_setup.py:1453-1471`
- **Category:** correctness
- **What's wrong:** The `/_apx/wizard/tables` endpoint returns `{"tables": [...], "warehouse_id": …}`, but `loadTables` does `const tables = await r.json()` then checks `!tables.length` on the object (always falsy) → always renders "No tables found" and never builds tool proposals, breaking wizard steps 2 and 3. The `_render_setup_ui` copy was already fixed for exactly this bug (and carries a comment about it); the wizard path was missed.
- **Evidence:** Dev-only setup scaffolding (`include_in_schema=False`), but it completely breaks two wizard steps.
- **Recommendation:** `const data = await r.json(); const tables = data.tables || []` (mirror the fixed setup UI); surface `data.error`.
- **Tag:** `[verified]` — confidence **high**

#### H16. Unbalanced braces in `runEvalCase` break the entire chat dev-UI `<script>` block
- **File:** `python/src/apx_agent/_ui_chat.py:976-982`
- **Category:** correctness
- **What's wrong:** A brace mismatch in `runEvalCase()` (the `response.completed` `else if` is never closed before `} catch {}`) means the single inline `<script>` of the chat page fails to parse — chat submit, tool invocation, eval runner, and trace rendering all silently stop working. Empirically confirmed: rendering the page and running `node --check` on its largest inline script yields `SyntaxError: Unexpected token 'catch'`. The parallel `_render_eval_landing` block closes the brace correctly.
- **Recommendation:** Add the missing closing brace; add a smoke test that asserts the rendered inline script parses (headless / JS parser) to catch f-string brace drift.
- **Tag:** `[verified]` — confidence **high**

#### H17. Dev UI code-write endpoint (`/_apx/edit`) is always mounted with no auth or dev gating
- **File:** `python/src/apx_agent/_dev.py:781-814` (router included unconditionally at `_wiring.py:128-131`)
- **Category:** security
- **What's wrong:** `build_dev_ui_router()` is included with no env/dev flag and no per-route authorization. `POST /_apx/edit` accepts arbitrary `content`, does a *syntax-only* `compile()` check, writes it verbatim to `agent_router.py`/`agent.py`, and uploads it to the workspace source path — that file is imported and executed on restart, so any caller who can reach `/_apx/edit` achieves arbitrary Python execution in the agent runtime. The dev router is mounted on the deployed Apps path too. `/_apx/tools/new` and `/_apx/tools/suggest` splice LLM/user-supplied function bodies into the same file.
- **Evidence (why high, not critical):** In a deployed App the workspace SSO proxy authenticates callers, so this is authorization-bypass/privilege-escalation (any authorized App viewer can rewrite the agent) rather than unauthenticated internet-facing RCE.
- **Recommendation:** Gate the entire dev router behind an explicit opt-in so it is never mounted in deployed Apps by default; add an authorization dependency to all write endpoints.
- **Tag:** `[verified]` — confidence **high**

#### H18. SSRF: probe fetches any user-supplied URL from inside the deployment
- **File:** `python/src/apx_agent/_dev.py:1554-1571` (and `_ui_probe.py:931-957`)
- **Category:** security
- **What's wrong:** `GET /_apx/setup/probe` and `_run_probe` take a raw `url` query param and issue a server-side `httpx.get(url, follow_redirects=True)` with no allowlist, scheme restriction, or private-IP/metadata filtering. From inside a Databricks App this lets an authorized caller reach cloud instance-metadata, private-link services, and (via redirects) targets unreachable from their browser.
- **Evidence:** Behind Apps front-door auth, so the attacker is an authorized app user, not anonymous — but the SSRF pivot via the app's network identity is real.
- **Recommendation:** Restrict to http/https, resolve and reject RFC1918/link-local/metadata IPs, bound/re-validate redirects, and gate behind the dev-only flag.
- **Tag:** `[verified]` — confidence **high**

#### H19. Transient session-store read failure silently wipes conversation history on next persist
- **File:** `python/src/apx_agent/_session_delta.py:118-145` (and `_session_lakebase.py:151-178`, `_session.py:132-139`, `_chat_agent.py:263-283`)
- **Category:** error-handling
- **What's wrong:** `get()` catches every exception and returns `None`, conflating "missing" with "backend errored," contradicting the protocol where `None` means missing. `load_or_create_session` treats `None` as new and immediately `put()`s an empty session; `put()` has no try/except and full-replaces history (`SET history = src.history` / `ON CONFLICT … SET history = EXCLUDED.history`). When the underlying row actually exists (transient read failure on a multi-replica backend — read-replica blip, stale pooled connection), the MERGE/UPSERT clobbers durable multi-turn history with `[]`. Irreversible.
- **Evidence (calibration):** The clobber is the *immediate* `put` at `_session.py:138`, not the turn-end persist seconds later (the finder's "separated by the agent run" rationale doesn't survive scrutiny). Sustained outages are *safe* (the put raises and propagates). The realistic trigger is the no-shared-fate class (read hits down replica → None; write hits up primary → success). High, not critical, because it needs a specific split-fate infra condition.
- **Recommendation:** Distinguish absence from failure — let infra exceptions propagate or wrap in a typed `StoreError`; reserve `None` for confirmed-missing. Do not create-and-put when the read failed.
- **Tag:** `[verified]` — confidence **high**

#### H20. Vector Search tool returns empty list on query failure — LLM reads it as "no results found"
- **File:** `python/src/apx_agent/vector_search.py:67-77` (merged: `integrations-security` + `error-handling-sweep` reported the same sink)
- **Category:** error-handling
- **What's wrong:** The vector-search tool callable (exposed directly to the LLM) catches every exception from `query_index` and returns `[]`. An unreachable index (private-link/FEVM block, expired token, index not READY, permission denied) is indistinguishable from a genuine zero-hit search, so the agent confidently answers "I found no matching documents" while retrieval is broken. The sibling `http_tools` returns `{"error": str(e)}` on failure, proving the codebase knows the better pattern — this is an inconsistency, not intended design. This is the canonical MLflow-blob-storage archetype.
- **Recommendation:** Return a structured error the model can see (e.g. `[{"error": …}]`) or raise so the tool-runner records a tool error. Never hand the LLM a bare `[]` semantically identical to a successful empty result.
- **Tag:** `[verified]` — confidence **high**

#### H21. Memory recall masks backend failure as "No memories found."
- **File:** `python/src/apx_agent/_memory_lakebase.py:489-494` (recall) and `446-451` (list); `_memory_delta.py:478-483`
- **Category:** error-handling
- **What's wrong:** `recall()`/`list()` catch all exceptions and return `[]`; the recall tool feeds that into `_format_recall_results`, which renders the literal "No memories found." So a Postgres outage, expired Lakebase OAuth token, or pgvector error makes the agent assert the user has no relevant durable memory — corrupting any answer that depends on long-term recall. The Delta store inherits the flaw via its SQL branches calling `list()`.
- **Recommendation:** Let `recall()`/`list()` raise (or return a distinguishable error) on infra failure so the tool can say "memory backend unavailable" instead of "No memories found." Apply to the Delta store too.
- **Tag:** `[verified]` — confidence **high**

---

### Medium

#### M1. `apx info` crashes (IndexError) when a tool has a whitespace-only docstring
- **File:** `python/src/apx_agent/cli.py:2961` (finder cited stale `2894`)
- **Category:** correctness
- A whitespace-only docstring (`"""   """`) is truthy, so `(fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""` runs `[].splitlines()[0]` → IndexError, producing a raw traceback from a core read-only command. Reproduced directly. **Fix:** compute `lines` first, then `lines[0] if lines else ""`. `[verified]` — confidence **high**

#### M2. SQL injection via `--table` / `APX_WATCHDOG_VIOLATIONS_TABLE` in `apx watchdog violations`
- **File:** `python/src/apx_agent/cli.py:4174-4197`
- **Category:** security
- Table name validated only by `table.count(".") != 2`, then interpolated raw into `FROM {table} WHERE …`. A value like `a.b.c WHERE 1=1 UNION SELECT …` has two dots and passes. The adjacent `agent_name` filter *is* escaped, showing the identifier was simply missed. Operator-sourced (CLI flag / CI env), so gated behind operator-controlled input. **Fix:** whitelist/backtick-quote each of the three identifier parts. `[verified]` — confidence **high**

#### M3. `check_project_layout` has a dead apps FAIL branch and a model-serving false-pass
- **File:** `python/src/apx_agent/_doctor.py:244-259`
- **Category:** correctness
- For apps, the check confirms only `agent_server/` is a dir (necessarily true whenever `_detect_target` returned "apps"), so the FAIL branch is unreachable and a missing `start_server.py` reports OK. For model-serving it checks `agent.py` while the runtime imports `app:app` (app.py), so a deleted `app.py` reports OK while `apx run` fails. **Fix:** check the real entrypoint files; add a model-serving layout-FAIL test. `[verified]` — confidence **high**

#### M4. `_parse_judge_output` flips unclear judge output to PASS, contradicting its FAIL-on-unclear contract
- **File:** `python/src/apx_agent/_dev.py:279-282`
- **Category:** correctness
- When the judge emits no `VERDICT:` line, a bare substring test (`"PASS" in upper and "FAIL" not in upper`) flips to PASS — so "passable", "would pass", or "COMPASSIONATE" score as PASS, the worst direction for an eval harness. Gated behind malformed (non-format-compliant) judge output, so not high. **Fix:** keep FAIL on no-VERDICT; if substring inference is kept, use `\bPASS\b`. `[verified]` — confidence **high**

#### M5. `apx run --reload` defeats the in-process MLflow autolog the dev Trace panel depends on (apps target)
- **File:** `python/src/apx_agent/cli.py:1229-1240`
- **Category:** correctness
- `autolog_if_env()` runs in the supervisor process; under `--reload` uvicorn re-imports the module in a worker subprocess that never re-runs `run()`'s body, and the apps lifespan does not call autolog itself, so per-tool/per-LLM spans aren't emitted. Default `apx run` (no reload) and the model-serving target are unaffected. **Fix:** have the apps lifespan/start_server call `autolog_if_env()` in-process. `[verified]` — confidence **high**

#### M6. `LlmAgent` `temperature` / `max_tokens` / `max_iterations` are silently ignored by the compile runtime
- **File:** `python/src/apx_agent/_compile.py:233-252`
- **Category:** dead-code / API-contract
- Stored on `__init__` but never read by `_compile_llm_agent` (model built from endpoint only; no recursion limit). The `run_via_compile` docstring falsely claims they are "encoded at compile time." Callers setting `temperature=0` get no effect. **Fix:** thread them into the compile path, or drop the params and fix the docstring. `[verified]` — confidence **high**

#### M7. Config `sub_agents` merge silently no-ops for non-`LlmAgent` roots
- **File:** `python/src/apx_agent/_wiring.py:86-101`
- **Category:** correctness
- `getattr(agent, "_sub_agent_urls", [])` returns a throwaway list for composition roots (only `LlmAgent` defines the attr), so config-declared `sub_agents` are silently dropped from the A2A/MCP discovery surface for those roots. (The second claim — never callable on `/invocations` — is intended architecture, not a defect.) **Fix:** fail loudly / warn when `config.sub_agents` is set on a root lacking the attribute. `[verified]` — confidence **high**

#### M8. `introspect_schema` interpolates schema name directly into SQL
- **File:** `python/src/apx_agent/_schema.py:28-38`
- **Category:** security
- `WHERE table_schema = '{schema}'` with no escaping; `schema` is reachable from the dev-UI setup wizard (`body.get("schema")`). A quote breaks the query / a crafted value injects. Runs under app credentials against read-oriented `information_schema`, and the result failure is swallowed to `{}`. **Fix:** bound parameter or `^[A-Za-z0-9_]+$` allowlist. `[verified]` — confidence **high**

#### M9. Canary analysis ignores the lookback window — `search_traces` called without any time filter
- **File:** `python/src/apx_agent/_canary.py:360-399` (and `_canary_apps.py:309-345`)
- **Category:** correctness
- `lookback_hours` is documented as the aggregation window but is only echoed on the report; the query passes no time bound, so aggregates include traces arbitrarily far outside the window (up to `max_traces=2000`). Both versions draw from the same unfiltered pool, so the comparison stays like-for-like but stale traffic can mask a regression. **Fix:** pass a `filter_string`/`start_time` from `now - lookback_hours`. `[verified]` — confidence **high**

#### M10. `deploy_canary` leaves traffic under-allocated when the endpoint has no existing served entities
- **File:** `python/src/apx_agent/_canary.py:247-280`
- **Category:** correctness
- With empty `seen`, neither traffic-distribution branch runs; only the canary route at `canary_traffic_pct` is appended and `remaining` is never assigned, so `TrafficConfig` sums to <100 and Model Serving rejects the deploy. The docstring frames the empty case as supported but only handles non-empty `seen`. **Fix:** route 100% to the canary when no existing entities, or raise a clear error. `[verified]` — confidence **high**

#### M11. `promote_canary_app` reports success even when the prod `bundle run` step fails
- **File:** `python/src/apx_agent/_canary_apps.py:533-561`
- **Category:** error-handling
- A non-zero prod `bundle run` is only a warning ("continuing"), then the canary App is torn down and success is returned. If prod genuinely failed to come up and the canary is deleted, the operator has neither working prod nor rollback target. `rollback_canary_app` delegates to the same function. Operator-driven, human-in-loop, recoverable — hence medium. **Fix:** verify prod is serving the promoted deployment (apps get health/active-deployment) before teardown. `[verified]` — confidence **high**

#### M12. Delta recall Vector Search branch silently drops `tags` and `min_importance` filters
- **File:** `python/src/apx_agent/_memory_delta.py:500-513`
- **Category:** parity-drift
- VS branches forward only `principal_id`/`namespace`; the SQL branches and Lakebase honor `tags`/`min_importance`. Enabling VS silently changes recall filtering — excluded results get returned. **Fix:** add the filters to the VS filter dict or post-filter. `[verified]` — confidence **high**

#### M13. `DeltaMemoryStore.delete` always returns True, never detects a miss (Python)
- **File:** `python/src/apx_agent/_memory_delta.py:442-456`
- **Category:** correctness
- Returns True whenever the DELETE doesn't raise, violating the protocol ("True on hit, False on miss") that InMemory and Lakebase honor. The `forget` tool reports success on a nonexistent id; `consolidate_memories` records phantom `deleted_ids`. **Fix:** count-before-delete or affected-rows; return False on zero matches. `[verified]` — confidence **high**

#### M14. `LakebaseMemoryStore` does not validate or quote `table_name` despite docstring claim
- **File:** `python/src/apx_agent/_memory_lakebase.py:244` (used at 323/366/409/441/484)
- **Category:** security
- `table_name` assigned verbatim and raw f-string-interpolated into every statement; the docstring falsely claims it is "quoted as a SQL identifier." Delta hardens via `_validate_table_name`; Lakebase does not. Constructor-controlled with a safe default, so exploitation needs the app to pass attacker-controlled config. **Fix:** validate against an identifier allowlist (mirror Delta) or fix the docstring. `[verified]` — confidence **high**

#### M15. `DeltaExampleStore.delete()` returns True even when the row does not exist
- **File:** `python/src/apx_agent/_example_delta.py:306-320`
- **Category:** parity-drift
- Same defect class as M13 for examples; InMemory and Lakebase honor "True on hit, False on miss." Observable via the `remove_example` tool (false "Removed") and `apx examples remove` (documented to exit non-zero on no match, but always exits 0). **Fix:** existence check or affected-rows. `[verified]` — confidence **high**

#### M16. `mine_examples` `min_score` filter is bypassed for turns with no score
- **File:** `python/src/apx_agent/_example_mining.py:252-257`
- **Category:** correctness
- The guard requires `score is not None`, so unscored turns (no `score_fn`, or it returned None) pass straight through and get written despite a requested quality floor — opposite of the store-side `min_score` semantics (NULL excluded). **Fix:** drop unscored turns when `min_score` is set, or require `score_fn`. `[verified]` — confidence **high**

#### M17. Vector Search `find_similar` branch silently drops `tags` and `min_score` filters
- **File:** `python/src/apx_agent/_example_delta.py:356-398`
- **Category:** correctness
- Same VS-filter-drop as M12, on the example store. VS path filters on `agent_id`/`intent` only; SQL/recency and Lakebase honor `tags`/`min_score`. **Fix:** translate into the VS filter dict or post-filter. `[verified]` — confidence **high**

#### M18. Chain-eval trace correlation is substring/first-match based and silently miscounts sub-agent coverage
- **File:** `python/src/apx_agent/_eval_chain.py:120-123, 163-167, 201-265`
- **Category:** correctness
- Three compounding flaws: `_trace_matches_request` is a substring check on stringified inputs with no trace consumption (substring collisions, reuse); duplicate request strings re-match the same first trace (double-count); `lookback_traces=50` silently truncates larger evalsets (undercount). The coverage dict is surfaced to the user via `chain-eval`. A best-effort diagnostic (the code labels it "best-effort"), not a production path. **Fix:** match on structured first-user-message, consume traces once bound, page or warn on truncation. `[verified]` — confidence **high**

#### M19. `hot_swap_model` rebuilds `ServedEntityInput` from a fixed field subset, silently wiping config
- **File:** `python/src/apx_agent/_hot_swap.py:104-133`
- **Category:** correctness
- Copies 9 of 15 `ServedEntityInput` fields and calls `update_config_and_wait` (full replace), dropping `external_model`, `instance_profile_arn`, `*_provisioned_throughput`, `provisioned_model_units`, `burst_scaling_enabled`. Note the finder's "compounding" claim is overstated — those fields exist on `ServedEntityOutput` under the *same* names, so a getattr carry-over would work; and the documented target (agents.deploy custom-model endpoints) is unlikely to carry the most catastrophic fields, though `instance_profile_arn`/`burst_scaling_enabled` could be wiped. **Fix:** round-trip all fields (e.g. `dataclasses.replace`) or use a partial-update API. `[verified]` — confidence **high**

#### M20. `_normalise_trace` reads `spans[0]` as the root without ordering guarantee; empty-span traces produce tag-only rows counted as success
- **File:** `python/src/apx_agent/_trace_export.py:112-117`
- **Category:** correctness
- `spans[0]` is assumed to be the root span (it is the one with no parent), so apx.* attributes may be read off a child span. Separately, a trace with empty spans yields a tags-only row that is still returned and counted under `rows_written` rather than skipped — interacts with H9 (blocked-blob empty-span traces). **Fix:** locate the root by absent parent_id; flag/skip metadata-only rows. `[verified]` — confidence **high**

#### M21. PopulationStore SQL escaping does not escape backslashes (unlike DeltaEngine); agent output flows into raw SQL
- **File:** `python/src/apx_agent/workflow/population_store.py:267-287`
- **Category:** security
- Local `_esc` doubles quotes only; the sibling `engine_delta._esc` escapes both. `decoded_sample`/`symbol_map`/etc. come from sub-agent (`_call_app`) output and are interpolated into a hand-built INSERT. The `update_fitness_scores` fallback (line 369) is weaker still — replaces apostrophes with double-quotes. Requires the no-Spark SQL fallback path and agent (not direct end-user) input. **Fix:** align `_esc` with `engine_delta` (backslash then quote); prefer the parameterized DataFrame path. `[verified]` — confidence **high**

#### M22. `streamViaSDK` does not actually stream — yields the whole final message as one chunk; `chatCompletionsStream` is dead code (TS)
- **File:** `typescript/src/agent/runner.ts:155-197, 415, 428-434`
- **Category:** correctness
- Documented as incremental, but calls the non-streaming `chatCompletions` and yields the full content as a single delta after generation completes; the real streaming primitive `chatCompletionsStream` is never called/exported. JSDoc contradicts behavior. **Fix:** wire through `chatCompletionsStream` and parse SSE deltas, or remove it and correct the JSDoc. `[verified]` — confidence **high**

#### M23. `doc_upload` allows path traversal in the UC Volume path via unsanitized filename (TS)
- **File:** `typescript/src/connectors/doc-parser.ts:91-100`
- **Category:** security
- `filename` (z.string(), no validation) is concatenated into the Files API path; reachable via the LLM tool loop and the raw POST tool endpoint. (Correction: the finder's "one leading `../` escapes" is wrong — the uuid prefix makes the first segment a literal dir name; a real escape needs an embedded separator so subsequent clean `..` segments appear.) Writes bounded by the token's permissions. **Fix:** reject separators/`..`, enforce a safe basename regex, assert the normalized path stays under `volumePath`. `[verified]` — confidence **high**

#### M24. Dev `/_apx/probe` SSRF blocklist bypassable via redirects and non-dotted IP encodings (TS)
- **File:** `typescript/src/dev/index.ts:65-110`
- **Category:** security
- Prefix-string blocklist on `hostname` plus a bare `fetch(url)` (default redirect: follow). Bypass via a public URL that 302s to `169.254.169.254`/`127.0.0.1`, or decimal/octal/hex encodings (`http://2130706433/`). Mitigated by `productionGuard` (dev-only) and the handler returning only status/ok/elapsed (blind SSRF, no body exfil) — hence medium not high. **Fix:** numeric IP-range check + `redirect: 'manual'` re-validation. `[verified]` — confidence **high**

#### M25. Watchdog UC violation INSERT escapes only single quotes — SQL breakout via trailing backslash (TS)
- **File:** `typescript/src/watchdog.ts:559-561, 635-648`
- **Category:** security
- `sqlStrLiteral` doubles quotes only; `engine-delta.ts` and `memory-lakebase.ts` both escape backslashes too, so watchdog is the outlier. Injected fields come from the watchdog MCP transport response, so the threat is a malicious/compromised MCP endpoint, not arbitrary end users. **Fix:** escape backslashes too, or use parameterized statements. `[verified]` — confidence **high**

#### M26. `DeltaMemoryStore.delete()` returns true even when no row was deleted (TS)
- **File:** `typescript/src/memory-delta.ts:323-333`
- **Category:** parity-drift
- TS counterpart of M13. Observable via the `forget` tool and `memory-consolidate` deletedIds. **Fix:** read the row first (like Lakebase) or use an affected-rows signal. `[verified]` — confidence **high**

#### M27. `DeltaMemoryStore.recall()` silently drops `tags` and `minImportance` in the Vector Search branch (TS)
- **File:** `typescript/src/memory-delta.ts:364-402`
- **Category:** correctness
- TS counterpart of M12. VS branch builds filters from `principal_id`/`namespace` only. **Fix:** add to filters or post-filter (tags array-intersection likely needs post-filtering; minImportance maps to a range filter). `[verified]` — confidence **high**

#### M28. `SequentialAgent.run` and `LoopAgent.run` leave the workflow run stuck in 'running' when a step throws (TS)
- **File:** `typescript/src/workflows/sequential.ts:62-94` (and `loop.ts:97-108`)
- **Category:** error-handling
- No try/catch around the step loop; `engine.step` re-throws on failure before `finishRun()`, so the run record stays 'running' permanently and failed runs are indistinguishable from in-flight in `listRuns`/`getRun`, breaking monitoring/resume. **Fix:** wrap the loop, call `finishRun(runId, 'failed', …)` before re-throwing. `[verified]` — confidence **high**

#### M29. Delta memory store: missing `index_name` with `vector_search` fails fast in Python but silently does the wrong recall in TS (parity)
- **File:** `typescript/src/memory-delta.ts:184-199, 368`
- **Category:** parity-drift
- Python raises `ValueError` when `vector_search` is set without `index_name`; TS has no such guard and silently falls through to the client-side cosine path (`limit: 10_000` SQL pull + local ranking). Developer-misconfiguration time, returns plausible results — hence medium. **Fix:** add a constructor guard in TS mirroring Python. `[verified]` — confidence **high**

#### M30. Lakebase recall on embedding-dimension mismatch: TS throws, Python returns empty list (parity)
- **File:** `python/src/apx_agent/_memory_lakebase.py:454-494`
- **Category:** parity-drift
- TS validates query-embedding length and throws; Python performs no dimension validation and a mismatch surfaces as a swallowed DB error → `[]`, with the cause only in a warning log. TS also validates add/addBatch/update; Python validates none. Manifests only with a misconfigured embedder. **Fix:** add embedding-dim validation to the Python Lakebase store so mismatches raise. `[verified]` — confidence **high**

#### M31. Non-streaming output reconstruction diverges: Python returns full message trail, TS returns single assistant message (parity)
- **File:** `typescript/src/responses-agent.ts:320-340`
- **Category:** parity-drift
- Python emits `function_call`/`function_call_output` items; TS returns only a single assistant message from the runner's final text (the runner type is `Promise<string>`, so it structurally cannot surface intermediate items). Partly by design. Note: the finder claims this is documented in the TS "known gaps" header, but that header documents only the *streaming* caveat, not the non-streaming output-array shape. **Fix:** surface intermediate tool items in the TS runner, or document the non-streaming gap explicitly. `[verified]` — confidence **high**

#### M32. Evolutionary workflow engine (`workflow/loop_agent.py`, 643 LOC) is entirely untested
- **File:** `python/src/apx_agent/workflow/loop_agent.py:223-643`
- **Category:** test-gap
- A distinct `LoopAgent` from the well-tested DSL primitive; its generation loop, control surfaces (pause/resume/force_escalate), fitness merging, and pure scoring functions (`composite_fitness`, `_local_statistical_fitness`) have zero coverage. Live shipped code (used by `workflow/cli.py` Databricks Workflows entrypoints). A coverage gap, not a demonstrated runtime defect. **Fix:** unit-test the pure functions first, then loop control methods; or remove if experimental. `[verified]` — confidence **high**

#### M33. XSS in Probe health-check rendering (name/message/hint injected unescaped)
- **File:** `python/src/apx_agent/_ui_probe.py:911-920`
- **Category:** security
- `c.name`/`c.message`/`c.hint` interpolated into `innerHTML` with no escaping; sub-agent check names/messages embed configured URLs and `str(exc)[:200]` strings that can reflect remote-server-controlled text. Dev-UI origin also hosts the code-edit endpoints. Leans toward self-XSS / config-poisoning, but exception strings can carry remote content. **Fix:** escape via a helper or build nodes with `textContent`. `[verified]` — confidence **high**

#### M34. `esc()` does not escape double quotes, enabling attribute-context XSS in the eval judge badge
- **File:** `python/src/apx_agent/_ui_chat.py:849`
- **Category:** security
- `esc()` escapes `&<>` but not `"`; its output is placed inside `title="${esc(r.judge_reason || '')}"`. `judge_reason` comes from the LLM judge / persisted `evals.json`, so a `"` can break out and inject event-handler attributes. A correct `escHtml()` exists at line 1127. Dev-only surface. **Fix:** escape `"` in `esc()` or use `escHtml()` for attribute contexts. `[verified]` — confidence **high**

---

### Low

> Low-severity items tagged `[verified]` were independently confirmed; items tagged `[UNVERIFIED]` are pass-throughs not re-verified by the skeptical reviewer.

**Verified low:**

- **L1.** IndexError misreports an `apx test` prompt as a FAIL when the assistant returns whitespace-only text — `cli.py:3418`. The finder's "uncaught traceback crash" claim is **false**: the IndexError is caught by the surrounding `except` and merely misreported as a confusing failure (and the "same fix at 2894" note is bogus). correctness. `[verified]` high.
- **L2.** Malformed evalset surfaces a raw `JSONDecodeError` instead of a clean `click.ClickException` — `cli.py:1396-1399` and `3731-3734` (finder cited stale lines). Purely cosmetic CLI UX inconsistency vs `_read_databricks_yml`. error-handling. `[verified]` high.
- **L3.** `_compile_run._to_langchain` drops assistant `tool_calls` and emits empty `tool_call_id` — `_compile_run.py:117-120`. **Reachability refuted** by the verifier: `/invocations` and chat routes use other converters; this path is reached only via the public `BaseAgent.run/.stream`, and the `Message` model has no `tool_calls` field, so no valid linkage could be represented. The hardcoded empty `tool_call_id` is a real latent defect; downgraded from medium. correctness. `[verified]` high.
- **L4.** Untrusted remote MCP tool **descriptions/results** injected into LLM context — `mcp_consume.py:154-162, 234-245`. Corrections: tool *names* are slugified (not verbatim, contrary to the finder), and the finder's quoted "module docstring" warning is **fabricated** (does not exist in the file). Inherent property of consuming any MCP server, developer opt-in, OBO already host-guarded — a defense-in-depth gap. security. `[verified]` high.
- **L5.** OBO token forwarded on hostname-only match, ignoring port and scheme — `mcp_consume.py:85-89, 128-138`. `_same_host` compares only hostname, so a cleartext `http://` downgrade or alternate port of the same host receives the token. Both inputs are operator/developer-controlled, not runtime input. security. `[verified]` high.
- **L6.** RateLimit per-principal token state grows unbounded — `_guards.py:83-100`. Default `principal_key=None` is safe (single bucket); leak only with request-derived keys, and per-entry cost is two floats. performance/hygiene. `[verified]` high.
- **L7.** Watchdog decision action not normalized — non-canonical reject/redact fail open to allow — `_watchdog.py:224-231, 319-348`. Triggers only with an out-of-contract transport (shipped transports are self-consistent); partial span visibility exists. security. `[verified]` high.
- **L8.** `voynich-seed` fallback writes a class dunder string as `cipher_type` — `workflow/cli.py:122-123`. Correction: only index `[0]` (the `__module__` value) is garbage, not `[0]` and `[1]` — `[1]` is the valid `substitution` constant, so blast radius is ~1/6, half the finder's claim. Always-true `hasattr` guard is dead code. correctness. `[verified]` high.
- **L9.** Tool name/description rendered unescaped into HTML and an inline `onclick` (DOM XSS) — `_ui_setup.py:491-503`. Dev-only; input authored by the developer (self-XSS dominant). security. `[verified]` high.
- **L10.** Env "current tag" values interpolated unescaped into setup-page HTML — `_ui_setup.py:190-198`. Writable via `save_setup` (strip-only), but same principal writes and views (self-XSS). security. `[verified]` high.
- **L11.** XSS in Edit-page schema preview (tool name/description/error into `innerHTML`) — `_ui_edit.py:1294-1309`. Dev-only editor whose purpose is writing executable source on the same origin; self-XSS. security. `[verified]` high.
- **L12.** `uv.lock` proxy auto-fix and deploy sanitization match the exact URL while the CI guard matches the host substring — `scripts/check-uv-lock-registry.sh:22-24`. Asymmetric: a path/scheme variant trips CI red but `--fix`/`apx deploy` no-op. Latent today (all layers align on `/simple`). maintainability. `[verified]` high.
- **L13.** Unguarded `new URL(agent.url)` in render with no router error boundary (hub) — `routes/agents/$agentId.tsx:185`. Real but near-unreachable: every ingress that stores a non-empty `agent.url` is crawl-gated, and any URL `httpx` can GET also parses in `new URL()`. The "blanks the whole app" mechanism is **unverified** (TanStack Router catch-boundary behavior couldn't be confirmed without node_modules). error-handling. `[verified]` **medium confidence**.
- **L14.** Unvalidated scaffold project `name` (TS CLI) — `cli/commands/scaffold.ts:410-415, 444-468`. **Security framing refuted**: `name` is a developer-typed CLI positional, no trust boundary, so the path-escape is not a vuln. Remains a real robustness defect (malformed names produce broken `databricks.yml`/`package.json`). security→robustness. `[verified]` high.

**Unverified low (pass-throughs):**

- **L15.** Unescaped `--agent` value breaks the MLflow trace filter string — `cli.py:2986`. correctness. `[UNVERIFIED]` low.
- **L16.** Dev UI `information_schema` queries built by f-string interpolation of catalog/schema — `_dev.py:846-857`. security. `[UNVERIFIED]` low.
- **L17.** `_safe_id` collisions can merge distinct agents/targets into one Mermaid node — `_topology.py:263-267`. correctness. `[UNVERIFIED]` low.
- **L18.** `/invocations` stream errors emit an `event: error` SSE frame outside the data-only contract — `_invocations.py:183-186`. correctness. `[UNVERIFIED]` low.
- **L19.** Deprecated `_discovery` module is dead-code shim — `_discovery.py:1-11`. dead-code. `[UNVERIFIED]` low.
- **L20.** `openapi_tool` fetches a developer-supplied URL with no response-size limit — `http_tools.py:172-196`. error-handling. `[UNVERIFIED]` low.
- **L21.** `run_sql` passes `type=None` when `type` omitted, weakening bind typing — `_sql.py:162-167`. correctness. `[UNVERIFIED]` low.
- **L22.** UC function numeric SQL literal returns the original unparsed string — `catalog.py:30-37`. correctness. `[UNVERIFIED]` low.
- **L23.** `publish_to_supervisor` spreads `extra_tool_kwargs` verbatim into the Tool dataclass — `_publish.py:169-180`. security. `[UNVERIFIED]` low.
- **L24.** `consolidate_memories` deletion is non-atomic with the consolidation write — `_memory_consolidate.py:163-169`. correctness. `[UNVERIFIED]` low.
- **L25.** `append_turn` dedupes by object identity, double-appending equal-but-distinct dicts across a store round-trip — `_session.py:159-165`. correctness. `[UNVERIFIED]` low.
- **L26.** `LakebaseSessionStore` docstring example uses a method that doesn't match the documented auth API — `_session_lakebase.py:71-98`. maintainability. `[UNVERIFIED]` low.
- **L27.** Violation-writer table-create flag set even when CREATE TABLE fails, permanently disabling inserts — `_watchdog.py:661-721`. error-handling. `[UNVERIFIED]` low.
- **L28.** Cost breakdown labels genuinely-free SKUs as "unknown price," suppressing the total — `_cost.py:144-167`. correctness (by-design tradeoff). `[UNVERIFIED]` low.
- **L29.** `autolog_if_env` default-on for `apx run` enables langchain.autolog (~30s/run) and could leak into deploy — `_mlflow_tracing.py:101-141`. performance. `[UNVERIFIED]` low.
- **L30.** `_write_env_file` writes values verbatim — no quoting/escaping of newlines/special chars — `_ui_setup.py:36-53`. correctness. `[UNVERIFIED]` low.
- **L31.** Dead try/catch in `runner.resolveToken` — async rejection can't be caught synchronously (TS) — `runner.ts:87-94`. error-handling. `[UNVERIFIED]` low.
- **L32.** Leftover debug `console.log` on every write in `PopulationStore.writeHypotheses` (TS) — `population.ts:68-71`. maintainability. `[UNVERIFIED]` low.
- **L33.** `deploy` command try/catch whose branches are identical (no-op) (TS) — `cli/commands/deploy.ts:454-470`. dead-code. `[UNVERIFIED]` low.
- **L34.** `apps-smoke-test` never sanitizes the regenerated example `uv.lock` before deploy — `.github/workflows/apps-smoke-test.yml:126-131`. parity-drift. `[UNVERIFIED]` low.
- **L35.** `run_sql()` bind-parameter path (the documented injection mitigation) is never tested — `_sql.py:162-167`. test-gap. `[UNVERIFIED]` low.
- **L36.** Lakebase store docstrings claim `table_name` is "quoted as a SQL identifier" but it is interpolated raw; no test pins either behavior — `_session_lakebase.py:74-75`. parity/test-gap. `[UNVERIFIED]` low.
- **L37.** Lakebase `isoToEpoch` on blank/invalid timestamp: Python yields 0, TS yields current time (parity) — `memory-lakebase.ts:215-219`. parity-drift. `[UNVERIFIED]` low.
- **L38.** Delta VS score fallback: Python reads `similarity_score`, TS does not (parity) — `memory-delta.ts:396`. parity-drift. `[UNVERIFIED]` low.
- **L39.** `selectedAgent` stored as a stale object snapshot; chat panel keeps rendering a deregistered agent (hub) — `routes/index.tsx:81,141-142`. correctness. `[UNVERIFIED]` low.
- **L40.** Affirmative result (informational): no `dangerouslySetInnerHTML`, no hardcoded secrets, all hub client fetches are same-origin relative paths — `hub/src/agent_hub/ui/`. security (clean). `[UNVERIFIED]` low.

---

## Cross-Cutting Themes

### 1. Backslash-blind SQL string escaping (the single most pervasive defect class)
Five separate write paths double single quotes but never escape backslashes, while Spark/Databricks SQL treats backslash as an escape character by default. Backslash-terminated or `\'`-bearing content breaks out of the literal:
- `_memory_delta._quote_string` (H6) — also the shared helper behind the `_example_delta` half of H7, so one fix covers both.
- `_session_delta._sql_str` + `_example_delta` (H7) — reachable from request `session_id` and mined conversation text.
- `_trace_export._escape_sql` (H8) — request `session_id`, stored/second-order.
- `workflow/population_store._esc` (M21) — sub-agent output.
- `typescript/watchdog.ts sqlStrLiteral` (M25) — MCP transport output.
The codebase already has the *correct* helper in `engine_delta._esc`, `memory-lakebase.ts`, and the `run_sql`/Lakebase bind-parameter paths — the gap is non-uniform application. Preferred systemic fix: route all Delta/Spark writes through parameterized statements; where impossible, centralize a single escaper that handles backslash-then-quote and use it everywhere.

### 2. Silent failure on governed-data reads (error-swallowing that misleads the model or operators)
A recurring pattern of `except Exception → return []`/empty that erases the distinction between "no data" and "backend failed":
- Vector search tool (H20) and memory recall (H21) hand the LLM empty results → confident "no results"/"no memories" hallucinations on governed data.
- Session-store reads (H19) conflate missing with errored → history clobber.
- Canary/eval/trace reads (H9) return clean empty reports on blocked blob storage → can greenlight bad rollouts; the probe's write-path-only error capture gives false "ok."
- Lesser instances: `introspect_schema` → `{}` (M8), violation-writer latch (L27).
The fix posture is consistent: distinguish absence from failure, surface structured errors the model/operator can see, and the codebase's own `http_tools` error-return and `genie` `{"error": …}` convention show the right pattern already exists.

### 3. The dev UI (`/_apx/*`) as an unguarded, unconditionally-mounted security surface
The dev router is included with no env/dev flag and no auth dependency, yet hosts a code-write/RCE-equivalent endpoint (H17), an SSRF probe (H18), and multiple XSS sinks (M33, M34, L9–L11, L16). Mounted on the deployed Apps path too. The single highest-leverage hardening is gating the entire router behind an explicit opt-in so it is never mounted in deployed Apps, plus per-route authorization on write endpoints.

### 4. Python ↔ TypeScript behavioral drift
The two runtimes diverge in ways that change agent behavior for the same configuration:
- Streaming session handling (H13), non-streaming output shape (M31), VS-filter dropping (M12/M27), `delete()`-returns-true-on-miss (M13/M26), `vector_search`+`index_name` guard (M29), embedding-dim validation (M30), timestamp/score fallbacks (L37/L38).
Several of these are the *same* logical defect present in both languages (delete contract, VS filter drop), and several are one language doing the right thing while the other silently does not. A parity test matrix across the store/agent contracts would catch most of these.

### 5. OBO-token forwarding to under-validated destinations
Three independent paths forward the user's on-behalf-of token: the hub invoke (C2, unauth-registrant-controlled, critical), `RemoteDatabricksAgent` card rewrite (H5, operator-configured, high), and MCP hostname-only matching (L5, scheme/port-blind). Common fix: never forward credentials to a destination not pinned to a trusted origin/allowlist.

### 6. "The safe pattern exists one module away"
Beyond the above, this recurs structurally: `_validate_table_name` exists on Delta but not Lakebase (M14); the `escHtml`/`esc()` HTML escapers exist on one page but aren't applied on others (M34, L9–L11); the `include_spans=False` workaround exists in `_dev.py` but not the four sibling read sites (H9); `_safe_name` guards `start_run` but not `step` (H14). The codebase's quality ceiling is high; the failures are consistency failures.

### 7. Supply-chain / lockfile fragility
The Makefile hash-clobber (C1, critical), the detector/fixer breadth asymmetry (L12), and the smoke-test sanitization gap (L34) all concern `uv.lock` integrity. The `.build/uv.lock` corruption is untracked, which is precisely why CI never surfaced it — a reminder that generated-but-deployed artifacts need their own verification.

---

## Coverage & Method

**Subsystems audited (22 finders).** Each was adversarially re-verified by a skeptical second agent before inclusion here:
`cli`, `doctor-dev`, `agent-runtime`, `wiring-topology`, `tools-exec-security`, `integrations-security`, `auth-publish-deploy`, `persistence-memory`, `persistence-session-example`, `eval-guards-watchdog`, `observability-tracing`, `dev-ui-setup-nav`, `dev-ui-chat-edit-probe`, `workflow-engine`, `ts-runtime-tools`, `ts-workflows-persistence`, `ts-cli-dev`, `supply-chain-ci`, `parity-drift`, `error-handling-sweep`, `test-coverage-gaps`, `hub-frontend`.

**Counts.** Raw confirmed findings: 99. One true duplicate was merged — `integrations-security`'s vector-search swallow-to-empty (medium) and `error-handling-sweep`'s same-file/same-lines finding (high) are the identical sink; merged into H20 at the higher severity, citing both sources. After the merge: **97 distinct findings — 2 critical, 21 high, 34 medium, 40 low.**

**Raw-vs-refuted cannot be computed from this dataset.** The input is the *confirmed set only*; there is no record here of findings the finders raised and the verifiers refuted or dropped. What this dataset *does* show is that verification materially narrowed and corrected many confirmed findings rather than rubber-stamping them — multiple severities were downgraded with reasons (e.g. the `apx test` "crash" → caught-and-misreported; several mediums → low after refuted reachability), many cited line numbers were stale and are corrected above, at least one quoted docstring was fabricated (L4), and several exploit specifics were overstated (MCP names are slugified; `doc_upload` traversal detail; the `voynich-seed` blast radius is half the claim). Where evidence is thin or a headline overstates impact, the finding above says so.

**Verification depth and caveats.**
- The 2 critical, 21 high, and 34 medium findings carry `verified: true` — confirmed line-by-line against source by the skeptical reviewer.
- Of the 40 low findings, 14 are independently `[verified]` and 26 are `[UNVERIFIED]` pass-throughs (low-severity items were not re-verified by design). Treat the unverified lows as plausible-but-unconfirmed; spot-check before acting.
- One verified finding (L13, hub `new URL`) is verified-true but **medium confidence** — the defect is real but its blanking mechanism and real-world reachability are bounded/unconfirmed.

**Best-effort, not exhaustive.** Each finder operated under per-finder caps, so coverage within a subsystem is best-effort rather than complete; the absence of a finding in an area is not evidence the area is clean. The severity values used here are the verifier-*adjusted* severities, not the finders' original ratings. Within each tier, findings are grouped by subsystem/theme for readability rather than by a false-precision confidence ordering (confidence is near-uniformly high among verified items); verified-before-unverified ordering is applied only in the Low tier, where the unverified pass-throughs live.
