# AppKit Inline Developer Toolbar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the complete five-tab Erskine-style inline developer toolbar to the contract-parsing AppKit example, with controls that mutate and inspect the real AppKit runtime.

**Architecture:** A process-local control object in the internal TypeScript AppKit runtime derives fresh `AgentDefinition` objects from the compiled APX manifest. Generated host routes apply changes through `appkit.agents.register`; the client uses those routes for configuration and AppKit's existing user-scoped thread routes for sessions. `APX_DEV_UI=0` prevents the generated control routes from being registered and hides the client toolbar.

**Tech Stack:** TypeScript 5.9, AppKit 0.66.1 beta agents plugin, Zod 4, React 18, Tailwind CSS, Vitest, React Testing Library, Python host generation, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-appkit-inline-dev-toolbar-design.md`

## Global Constraints

- The toolbar and its control routes are enabled unless `APX_DEV_UI=0`.
- Development overrides are process-local and reset on restart or redeploy.
- Existing compiled APX tools continue through `InternalApxAppKitGovernancePlugin`; do not add another dispatcher.
- Session reads and deletion use AppKit's user-scoped `/api/agents/threads` routes.
- A new Databricks resource cannot be presented as live unless it is already declared and authorized by the deployed manifest.
- Add no dependencies and do not change APX policy, audit, OBO propagation, or full-console behavior.
- Use explicit `--profile fevm` for every Databricks command.

---

### Task 1: Mutable AppKit development definition

**Files:**
- Modify: `typescript/src/internal/appkit-host.ts`
- Test: `typescript/tests/appkit-host.test.ts`

**Interfaces:**
- Consumes: `InternalApxAppsHostManifest`, `createInternalApxAppKitAgentDefinitionFromManifest`, and the existing APX governance toolkit.
- Produces: `createInternalApxAppKitDevRuntime(manifest)`, whose result exposes `snapshot()`, `definition()`, `setModel(model)`, `setInstructions(instructions | null)`, `setToolEnabled(name, enabled)`, `setSkill(skill)`, and `deleteSkill(name)`.
- Produces: `internalApxAppKitSystemPrompt(instructions, context)` used both by the live definition and Prompt tab.

- [ ] **Step 1: Write failing prompt and runtime-state tests**

Add focused cases to `typescript/tests/appkit-host.test.ts` that construct a manifest, create the wished-for runtime, and assert:

```ts
const dev = createInternalApxAppKitDevRuntime(makeManifest());
expect(dev.snapshot()).toMatchObject({
  model: 'databricks-claude-sonnet-4-5',
  instructions: 'Use APX governed tools.',
  instructionsOverridden: false,
});
dev.setModel('databricks-claude-sonnet-4-6');
expect(dev.definition()).toMatchObject({ model: 'databricks-claude-sonnet-4-6' });
dev.setInstructions('Prefer concise answers.');
expect(dev.definition()).toMatchObject({ instructions: 'Prefer concise answers.' });
dev.setInstructions(null);
expect(dev.definition()).toMatchObject({ instructions: 'Use APX governed tools.' });
```

Add separate cases proving an existing tool can be disabled, an unknown tool is rejected, an authored skill appears as a real no-argument read-only tool, invalid or oversized skill input is rejected, and `snapshot().systemPrompt` equals the prompt configured by `definition().baseSystemPrompt` plus active instructions.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd typescript && npm test -- tests/appkit-host.test.ts
```

Expected: FAIL because `createInternalApxAppKitDevRuntime` and prompt helpers do not exist.

- [ ] **Step 3: Implement the minimal process-local runtime**

In `appkit-host.ts`, keep state in one closure. Validate model/instruction/skill boundaries with Zod, keep enabled tools in a `Set`, and build fresh definitions with the existing `createAgent` primitive. Compiled tools must still come from:

```ts
plugins[INTERNAL_APX_APPKIT_PLUGIN_NAME].toolkit({
  prefix: manifest.appkit?.tool_prefix ?? 'apx.',
  only: [...enabledTools],
})
```

Add authored skills as inline `tool({ schema: z.object({}), execute: async () => content })` entries with `{ effect: 'read', requiresUserContext: false }`. Use the explicit APX base prompt for both `definition().baseSystemPrompt` and `snapshot().systemPrompt` so inspection and execution cannot drift.

- [ ] **Step 4: Run the focused TypeScript test and verify GREEN**

Run:

```bash
cd typescript && npm test -- tests/appkit-host.test.ts
```

Expected: PASS.

- [ ] **Step 5: Run TypeScript typecheck and lint**

Run:

```bash
cd typescript && npm run typecheck && npm run lint
```

Expected: both commands pass without warnings introduced by the change.

- [ ] **Step 6: Commit the runtime slice**

```bash
git add typescript/src/internal/appkit-host.ts typescript/tests/appkit-host.test.ts
git commit -m "feat: add AppKit live dev runtime"
```

### Task 2: Generated AppKit control routes

**Files:**
- Modify: `python/src/apx_agent/_appkit_host_generator.py`
- Test: `python/tests/test_appkit_host_generator.py`
- Test: `python/tests/test_appkit_examples.py`

**Interfaces:**
- Consumes: `createInternalApxAppKitDevRuntime(manifest)` from Task 1 and `appkit.agents.register(name, definition)` from AppKit 0.66.1.
- Produces HTTP routes under `/api/dev`: `GET /config`, `PATCH /config`, `GET/PATCH/DELETE /instructions`, `GET /tools`, `PATCH /tools/:name`, `PUT/DELETE /skills/:name`, and `GET /prompt`.

- [ ] **Step 1: Write failing generated-host assertions**

Extend `test_writes_generated_appkit_host_skeleton` to require the generated server to import and construct the dev runtime, gate registration with:

```ts
const devEnabled = process.env.APX_DEV_UI !== '0';
```

and call `await appkit.agents.register(apxManifest.agent.name, dev.definition())` after mutations. Assert the proxy loop skips `/api/dev` so the Node routes win over the Python `/api` proxy.

Add an example-host assertion that `APX_DEV_UI=0` does not expose `/api/dev/config` and the default environment does.

- [ ] **Step 2: Run the focused Python tests and verify RED**

Run:

```bash
cd python && uv run --frozen pytest tests/test_appkit_host_generator.py tests/test_appkit_examples.py -q
```

Expected: FAIL because the generated host has no dev runtime or routes.

- [ ] **Step 3: Generate the minimal routes**

Update `_server_ts()` to create the runtime once, mount routes only when enabled, validate request bodies with Zod, and translate validation/unknown-tool failures to HTTP 400. Every successful model, instruction, tool, or skill mutation must await:

```ts
await appkit.agents.register(apxManifest.agent.name, dev.definition());
```

Return `dev.snapshot()` after mutation. Ensure the generic `/api` Python proxy does not intercept `/api/dev`; preserve all other configured proxy paths.

- [ ] **Step 4: Run focused generated-host tests and verify GREEN**

Run:

```bash
cd python && uv run --frozen pytest tests/test_appkit_host_generator.py tests/test_appkit_examples.py -q
```

Expected: PASS, including the real staged AppKit host process checks.

- [ ] **Step 5: Commit the generated-host slice**

```bash
git add python/src/apx_agent/_appkit_host_generator.py python/tests/test_appkit_host_generator.py python/tests/test_appkit_examples.py
git commit -m "feat: expose AppKit dev controls"
```

### Task 3: Typed five-tab toolbar

**Files:**
- Create: `python/examples/contract-parsing-agent/client/src/devApi.ts`
- Create: `python/examples/contract-parsing-agent/client/src/DevToolbar.tsx`
- Create: `python/examples/contract-parsing-agent/client/src/DevToolbar.test.tsx`
- Modify: `python/examples/contract-parsing-agent/client/src/App.tsx`
- Modify: `python/examples/contract-parsing-agent/client/src/App.test.tsx`

**Interfaces:**
- Consumes: Task 2 `/api/dev` routes and AppKit `/api/agents/threads` routes.
- Produces: `DevToolbar({ threadId, onReset })` and typed fetch helpers for configuration, instructions, tools, skills, prompt, list threads, delete thread, and reset all user threads.

- [ ] **Step 1: Write failing component behavior tests**

Create tests that render `App`, open `Dev`, and assert all tab buttons exist:

```ts
for (const tab of ['Config', 'Instructions', 'Tools', 'Sessions', 'Prompt']) {
  expect(screen.getByRole('tab', { name: tab })).toBeInTheDocument();
}
```

Add cases that select each tab and verify its API result, apply a model, apply/revert instructions, toggle a compiled tool, add/remove a markdown skill, list/delete sessions, render the effective prompt, reset the current session, close the panel, and retain the console link. Preserve the existing test proving an explicit `{ enabled: false }` removes the launcher.

- [ ] **Step 2: Run client tests and verify RED**

Run:

```bash
cd python/examples/contract-parsing-agent/client && npm test -- src/App.test.tsx src/DevToolbar.test.tsx
```

Expected: FAIL because the inline toolbar and typed API do not exist.

- [ ] **Step 3: Implement typed fetch helpers**

In `devApi.ts`, define the response types once and use one checked JSON helper:

```ts
async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`dev request failed: ${response.status}`);
  return response.json() as Promise<T>;
}
```

Sessions must call `/api/agents/threads` and delete with `/api/agents/threads/:id`; do not add a duplicate session backend.

- [ ] **Step 4: Implement the inline panel and App integration**

Create one focused `DevToolbar` using native buttons, inputs, textarea, checkbox, and tab roles. Load each tab on selection, disable submitting controls while requests are active, show inline errors/status, and state that overrides reset on restart. The Tools tab must distinguish compiled tools, live authored skills, and Databricks additions that require declaration/redeploy; the latter links to the APX console rather than pretending they are active.

Replace the current external launcher in `App.tsx` with the toolbar component while retaining the default-on `/api/dev-ui` behavior.

- [ ] **Step 5: Run client tests and build, verify GREEN**

Run:

```bash
cd python/examples/contract-parsing-agent/client && npm test && npm run build
```

Expected: all Vitest tests and the production TypeScript/Vite build pass.

- [ ] **Step 6: Commit the toolbar slice**

```bash
git add python/examples/contract-parsing-agent/client/src/App.tsx python/examples/contract-parsing-agent/client/src/App.test.tsx python/examples/contract-parsing-agent/client/src/DevToolbar.tsx python/examples/contract-parsing-agent/client/src/DevToolbar.test.tsx python/examples/contract-parsing-agent/client/src/devApi.ts
git commit -m "feat: add five-tab inline dev toolbar"
```

### Task 4: Current AppKit thread and reset integration

**Files:**
- Modify: `python/examples/contract-parsing-agent/client/src/useChat.ts`
- Modify: `python/examples/contract-parsing-agent/client/src/useChat.test.ts`
- Modify: `python/examples/contract-parsing-agent/client/src/App.tsx`

**Interfaces:**
- Consumes: the `thread_id` returned by AppKit `/invocations` and `DevToolbar({ threadId, onReset })` from Task 3.
- Produces: `useChat()` result containing `threadId` and `reset()` in addition to existing messages/loading/send behavior.

- [ ] **Step 1: Write failing hook tests**

Update the successful invocation fixture with `thread_id: 'thread-123'` and assert:

```ts
expect(result.current.threadId).toBe('thread-123');
act(() => result.current.reset());
expect(result.current.messages).toEqual([]);
expect(result.current.threadId).toBeNull();
```

Add an App test proving Reset Session deletes that thread and calls the hook reset callback only after a successful response.

- [ ] **Step 2: Run hook and App tests and verify RED**

Run:

```bash
cd python/examples/contract-parsing-agent/client && npm test -- src/useChat.test.ts src/App.test.tsx src/DevToolbar.test.tsx
```

Expected: FAIL because `threadId` and `reset` are absent.

- [ ] **Step 3: Implement thread capture and reset**

Store `data.thread_id` after each successful invocation and expose a stable `reset` callback that clears messages and the current thread id. Pass both into `DevToolbar` from `App`.

- [ ] **Step 4: Run the full client suite and build**

Run:

```bash
cd python/examples/contract-parsing-agent/client && npm test && npm run build
```

Expected: PASS.

- [ ] **Step 5: Commit the reset integration**

```bash
git add python/examples/contract-parsing-agent/client/src/useChat.ts python/examples/contract-parsing-agent/client/src/useChat.test.ts python/examples/contract-parsing-agent/client/src/App.tsx
git commit -m "feat: reset active AppKit session"
```

### Task 5: Repository proof, deployment, and pull request

**Files:**
- Modify only files already identified if validation finds a directly related defect.

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: a verified deployment of `contract-parsing-agent` and a focused GitHub PR to `main`.

- [ ] **Step 1: Run the TypeScript package gate**

```bash
cd typescript && npm test && npm run typecheck && npm run lint && npm run build
```

Expected: all commands pass.

- [ ] **Step 2: Run the generated AppKit example proof**

```bash
cd python && uv run --frozen pytest tests/test_appkit_examples.py -q
```

Expected: all generated examples stage, boot, and serve successfully.

- [ ] **Step 3: Run the repository read-after-write gate**

```bash
make check
```

Expected: the complete repository test/lint/sanitizer gate passes and leaves no unrelated lockfile churn.

- [ ] **Step 4: Inspect the final branch boundary**

```bash
git status --short
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
```

Expected: only the approved design, plan, internal AppKit runtime/generator tests, and contract example client are changed.

- [ ] **Step 5: Validate and deploy the contract example**

From `python/examples/contract-parsing-agent`, build the client and use the existing app deployment workflow with explicit profile:

```bash
databricks apps validate --profile fevm
databricks apps deploy -t dev --profile fevm
databricks apps get contract-parsing-agent --profile fevm -o json
```

Expected: validation succeeds and `app_status.state` is `RUNNING` at the existing app URL.

- [ ] **Step 6: Verify the authenticated deployed behavior**

Open the deployed app, verify the launcher is present, exercise all five tabs, apply and revert a harmless instruction override, toggle and restore one tool, confirm Prompt changes, confirm Sessions shows the actual AppKit thread, and reset it. Then inspect recent AppKit and Python bridge logs. Do not claim visual verification if authentication or browser access prevents it.

- [ ] **Step 7: Commit any directly related verification repair**

If deployment verification required a scoped fix, repeat its focused RED/GREEN test and commit only those files. Otherwise make no empty commit.

- [ ] **Step 8: Push and open the focused PR as `stuagano`**

In one shell process, switch and verify GitHub identity before external writes:

```bash
gh auth switch --user stuagano
active_login=$(gh api user --jq .login)
test "$active_login" = "stuagano"
git push -u origin codex/appkit-inline-dev-toolbar
gh pr create --base main --head codex/appkit-inline-dev-toolbar \
  --title "feat: add AppKit inline dev toolbar" \
  --body "## Summary
- add a five-tab inline AppKit developer toolbar
- apply model, instruction, tool, and skill changes to the registered AppKit agent
- inspect and reset user-scoped AppKit threads

## Verification
- TypeScript tests, typecheck, lint, and build
- generated AppKit example proof
- full make check
- deployed contract-parsing-agent verification with profile fevm"
```

Expected: a PR URL targeting `main` from `codex/appkit-inline-dev-toolbar`.

- [ ] **Step 9: Refresh live PR and CI state**

```bash
gh pr view codex/appkit-inline-dev-toolbar --json number,title,state,baseRefName,headRefName,mergeStateStatus,reviewDecision,isDraft,url,statusCheckRollup
```

Report local gates, deployed verification, current CI/review status, and any remaining external check separately.
