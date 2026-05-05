# Hub Chat — Two-Panel Interface Design

## Goal

Transform the Agent Hub index page into a two-panel "one stop shop" — agent list on the left, chat interface on the right — so users can browse and talk to any live agent without leaving the hub.

## Architecture

The index page (`routes/index.tsx`) is redesigned as a two-panel split. The left panel is the agent list (existing data, new selection behavior). The right panel is a new `ChatPanel` component that calls the existing `/api/agents/{id}/invoke` proxy. No new backend endpoints are needed. All live apx-agent apps get `supports_invoke=True` in the registry.

## Components

### `routes/index.tsx` (modify)

Replace the current grid layout with a two-panel flex layout:

- **Left panel (30%)** — agent list, grouped Live / In Development. Clicking a live agent sets it as `selectedAgent` (React state). Selected agent gets a highlighted border. Stub/in-development agents are shown but not clickable.
- **Right panel (70%)** — renders `<ChatPanel agent={selectedAgent} />`. If no agent selected, shows a "Select an agent to start chatting" empty state.

### `components/apx/ChatPanel.tsx` (new)

Self-contained chat component. Props: `agent: AgentCard`.

- **Header bar** — agent status dot, display name, description (truncated), "view details ↗" link to `/agents/{id}`.
- **Message thread** — list of `{ role: "user" | "agent", text: string }` messages in local state. User messages right-aligned, agent responses left-aligned. Resets when `agent.id` changes.
- **Empty state** — shown when no messages yet. Icon + short prompt hint + 2 suggested prompts derived from the agent's first two tool descriptions. Clicking a suggested prompt pre-fills the input.
- **Input bar** — text input + Send button. Enter key submits. Disabled while loading. On submit: append user message, call `POST /api/agents/{agent.id}/invoke` with `{ input }`, append agent response when it resolves. On error: show inline error message in the thread.

### `backend/router.py` (modify)

Set `supports_invoke=True` on all currently live apx-agent entries:
- `data-triage-agent`
- `data-triage-agent-ts`
- `data-inspector`
- `explain-my-bill`

`contract-parsing-agent` already has it. `entity-resolution-agent` stays `False` (stub).

### `lib/api.ts` (no change needed)

The existing `AgentCard` type already has `supports_invoke?: boolean`. The existing `/api/agents` endpoint already returns the full list. No new API functions needed.

## Data Flow

1. Page loads → `useListAgents()` fetches all agents → left panel renders list
2. User clicks a live agent → `setSelectedAgent(agent)` → `ChatPanel` mounts with that agent, message history clears
3. User types and clicks Send → `ChatPanel` POSTs to `/api/agents/{id}/invoke` → sets `loading=true`, input disabled
4. Response arrives → append `{ role: "agent", text: data.output_text }` → `loading=false`
5. Error → append `{ role: "agent", text: "Error: ..." }` styled as error

## Error Handling

- Invoke errors (non-2xx, network failure) show as an error message inline in the thread, styled distinctly. The input stays enabled so the user can retry.
- Agents without `supports_invoke` are not selectable from the left panel (greyed out, no click handler). This prevents sending to agents that don't expose `/responses`.

## What Doesn't Change

- The `/agents/$agentId` detail route stays as-is (tools list, connection info, TryItPanel).
- The `TryItPanel` on the detail page stays for agents with `supports_invoke=True` — it's a secondary entry point.
- Navbar stays as-is.
- No message persistence across page refreshes — in-memory only.
- No streaming — full response on completion.
