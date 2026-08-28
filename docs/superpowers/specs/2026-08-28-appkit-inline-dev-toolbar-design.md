# AppKit Inline Developer Toolbar

## Purpose

Give developers using an APX-generated Databricks App the same fast, inline
iteration loop as Erskine's PLG app while ensuring every control reflects the
AppKit runtime that actually serves the agent.

The toolbar is a development surface. It remains visible in deployed examples
unless `APX_DEV_UI=0` explicitly disables both its UI and control routes.

## User experience

A floating `Dev` button opens an inline panel without leaving the application.
The panel contains Reset Session, an `APX console` link, operation status, and
five tabs:

1. **Config** shows the agent name and active model. Applying a model override
   replaces the registered AppKit agent for subsequent invocations.
2. **Instructions** edits an in-memory override, applies it to the registered
   agent, or reverts to the compiled manifest instructions. Changing the prompt
   clears the requesting user's existing threads to avoid continuing with
   history created under a different prompt.
3. **Tools** lists compiled APX tools and allows each to be enabled or disabled
   live. Authored markdown skills are real read-only tools. A Databricks factory
   tool can become live only when its target resource and required authorization
   are already present in the deployed manifest; otherwise the UI labels it as
   requiring a redeploy instead of claiming it is active.
4. **Sessions** lists the requesting user's AppKit threads with message counts
   and timestamps. Reset deletes the actual AppKit thread and clears the local
   chat. Reset All affects only the requesting user's threads.
5. **Prompt** shows the exact effective base prompt, APX instructions, and tool
   surface used to assemble subsequent AppKit invocations.

The `APX console` link continues to open `/_apx/agent` for traces, topology,
probes, and durable source editing.

## Runtime architecture

The generated TypeScript AppKit host owns a process-local development state
initialized from `apx-host-manifest.json`. It contains only the current model,
instruction override, enabled compiled tool names, and authored development
skills or factory-tool proposals.

Every live configuration change creates a fresh `AgentDefinition` through the
existing internal APX AppKit definition builder and calls
`appkit.agents.register(agentName, definition)`. AppKit performs normal adapter
and tool-index construction, so `/invocations` and `/chat` immediately use the
replacement. The implementation does not add a second agent dispatcher or
bypass APX governance tool execution.

The generated server registers private development routes only when
`APX_DEV_UI` is not `0`. These routes read and mutate the TypeScript development
state, call the AppKit agents runtime, and use AppKit's user-scoped thread APIs.
The existing Python bridge remains responsible for compiled APX Python tool
execution and keeps OBO header forwarding, policy, and audit behavior intact.

Development overrides are intentionally ephemeral. A process restart or
redeploy restores the compiled manifest. The panel states this explicitly.

## Authorization and resource boundary

Enabling or disabling an already-compiled tool changes only the agent's tool
index; invocation still routes through `InternalApxAppKitGovernancePlugin`.

Markdown skills are local read-only tools and receive no Databricks client or
credentials. Their names and content are validated and bounded before storage.

Databricks Apps grants resources and OAuth scopes at deployment. The runtime
must not manufacture a live factory tool for an undeclared resource. Such an
entry remains pending and is clearly marked `redeploy required`; durable
resource/source editing belongs in the full APX console.

All session operations are user-scoped using AppKit's resolved request identity.
The toolbar never exposes or deletes another user's threads. Dev routes return
404 when disabled so turning off the UI also removes the mutation surface.

## Prompt inspection

The internal APX host supplies an explicit base-system-prompt function to the
agent definition rather than relying on an unexported AppKit helper. The same
function is used by the Prompt endpoint, together with the effective
instructions and resolved tool names. This makes the displayed prompt identical
to the prompt configured for the next AppKit invocation and keeps the behavior
testable through public AppKit contracts.

## Frontend structure

The contract-parsing client gains one `DevToolbar` component and a small typed
API module. `App` retains the default-on `/api/dev-ui` check, opens the toolbar
instead of navigating away, and passes the current AppKit thread id plus a chat
reset callback.

The panel uses the existing React, Tailwind, and native form controls. It adds no
frontend dependency. It includes keyboard-focusable tabs, labeled controls,
loading/error/empty states, and a close button.

## Testing

Implementation follows red-green-refactor:

- Component tests cover opening and closing, all five tabs, status/error states,
  reset behavior, and `APX_DEV_UI=0` hiding the surface.
- Hook tests prove the client retains the AppKit `thread_id` returned by an
  invocation and clears local messages after reset.
- TypeScript runtime tests prove model, instruction, tool, and skill changes
  replace the actual registered AppKit definition rather than only mutating UI
  state.
- Generated-host tests cover route presence, disabled-route absence, input
  validation, user-scoped sessions, reset, and exact prompt inspection.
- Existing contract client tests, TypeScript test/typecheck/lint/build, the
  generated AppKit example matrix, and `make check` form the local gate.
- Deployment uses explicit profile `fevm`, followed by app status/log checks and
  authenticated browser verification before the PR is presented as complete.

## Non-goals

- Replacing the full APX console.
- Persisting development overrides across a process restart.
- Granting new Databricks resources or OAuth scopes at runtime.
- Adding a second tool dispatcher or changing APX policy/audit semantics.
