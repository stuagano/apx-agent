# Agent Landing Page — Capability Cards + Declared Starter Prompts

**Status:** Design (approved). Next: implementation plan via writing-plans.

## Goal

Replace the deployed agent's bare empty-chat state ("Type a message…") with a landing that shows **what the agent is** and **what it can do**, sourced from the declarative agent definition so it works out of the box. Looking at an empty chat box teaches the user nothing; the data to do better (agent description + tool list) is already on the page.

## Background

The dev UI chat (`src/apx_agent/_ui_chat.py`) is the front page of every deployed apx-agent app (served at `/`). Today its empty state is just a placeholder textarea. The render already has access to:
- `ctx.config.name` and `ctx.config.description` (agent identity).
- `tools_json` / the client-side `TOOLS` array — each tool's `name`, `description`, and params (already rendered in a side "Tools" tab with invoke forms).

So this is a surfacing/layout change, plus one small declarative addition for author-curated starter prompts. It fits the agent-as-config philosophy: the landing auto-renders from the declarative agent.

## Design

### What renders (empty-chat state only)

The landing shows ONLY when the conversation is empty; it disappears once the first message is sent (same lifecycle as today's empty state).

1. **Greeting** — agent `name` + `description`. Always rendered (the one piece that's always present).
2. **"What I can do" — capability cards** — one card per tool, from the existing `TOOLS` data (`name` + `description`). **Clicking a card expands it inline** to show that tool's params/schema (reusing the tool metadata already in the page). Cards are **informational** — clicking does NOT send a prompt.
3. **"Try asking" — starter chips** — rendered from a new optional config field `examples` (see DSL addition). **Clicking a chip fills the chat input** (focused, not auto-sent) so the user can edit before sending.

### DSL addition (hybrid content source)

- New optional field in `[tool.apx.agent]`:
  ```toml
  examples = [
    "Show me a few sample customers",
    "Top 5 customers by account balance",
  ]
  ```
- Surfaces as `AgentConfig.examples: list[str]` (default `[]`), parsed from `pyproject.toml` alongside the existing `[tool.apx.agent]` fields.
- **UI-only metadata.** It does NOT affect runtime agent behavior — no `finalize_agent` / compile-path logic. It is carried through to the dev UI exactly like `description` is, and exposed to the chat render (e.g. `ctx.config.examples`).
- **Hybrid sourcing:** capability cards always render from tool metadata (zero config); the starter chips are the optional, author-curated layer. Absent `examples` → no chips, cards still render.

### Graceful degradation

| Agent shape | Landing |
|-------------|---------|
| tools + `examples` | greeting + capability cards + starter chips |
| tools, no `examples` | greeting + capability cards |
| no tools, `examples` set | greeting + starter chips |
| no tools, no `examples` (plain LLM agent) | greeting + input box (still better than a blank box) |

The greeting always renders; each of cards/chips renders only when its data is present.

### Scaffold

The scaffold's generated `[tool.apx.agent]` block (`_SCAFFOLD_APPS_PYPROJECT` in `cli.py`) ships with two real `examples` matching the default DataAgent's data source, so a freshly scaffolded agent gets a populated landing immediately.

## Components / files

- `src/apx_agent/_models.py` (+ the `[tool.apx.agent]` config loader): add the `examples: list[str]` field, parsed from pyproject (default `[]`, tolerant of absence / non-list).
- `src/apx_agent/_ui_chat.py`: the empty-state render — greeting + capability cards (expand-on-click) + starter chips (fill-input-on-click), with conditional rendering per the degradation table. Reuse the existing `TOOLS` data for cards (don't duplicate tool metadata).
- `src/apx_agent/cli.py`: `_SCAFFOLD_APPS_PYPROJECT` `[tool.apx.agent]` gains a sample `examples` list.

## Testing

- `AgentConfig` parses `examples` from pyproject (present → list; absent → `[]`; non-list/malformed → `[]` with no crash).
- Chat render: greeting always present; capability cards present iff tools present; starter chips present iff `examples` present; the no-tools-no-examples case renders greeting + input without error.
- Scaffold: generated `pyproject.toml` contains an `examples` entry under `[tool.apx.agent]`.
- (UI behavior — card expand, chip fill-input — is client-side JS; assert the rendered HTML/JS contains the wiring, consistent with how `_ui_chat.py` is tested today.)

## Out of scope

- Auto-generating example prompts from tool schemas (chips are author-declared only).
- Cards sending prompts (cards are informational/expand-only).
- Any change to the two serving adapters or the `/invocations` / `/responses` surface.
- Restyling the rest of the dev UI (chat transcript, tools tab, eval/trace panels).

## Open questions

None — all design decisions resolved during brainstorming (layout B; cards expand-only; examples hybrid/optional; chip click fills input).
