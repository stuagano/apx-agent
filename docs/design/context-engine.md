# Design: Context engine — dynamic prompt compilation for agent loops

**Status:** draft (issue #857) · **Date:** 2026-10-14

## Problem

Today's agent steps use **scaffolded prompts**: a fixed template with
`{{input}}`, `{{history}}`, `{{memory}}` slots filled before each model call.
This is "wired" — the developer writes one template and the runtime stuffs
data into it. The template cannot decide what to include or exclude based on
what the loop has learned so far.

A **context engine** compiles the prompt dynamically at each step boundary,
based on the current session state, task phase, and what context is actually
relevant. The prompt is a *product* of the loop, not a *template* for it.

## Thesis

The prompt is the most important control surface in an agent loop. A context
engine treats it as compiled output — assembled from modular context blocks,
gated by policy, ordered by relevance, and budgeted against the model's token
window — rather than a static string the developer writes once.

## What a context engine does

At each step, before the model call, the engine produces a **compiled prompt**
by running a pipeline of **context sources** through **policy gates** and a
**budget allocator**:

```
session state ──┐
task spec     ──┤
tool results  ──┼──► source collectors ──► policy filter ──► budget ──► compiled prompt
memory        ──┤                                              │
retrieved docs──┘                                              ▼
                                                         model call
```

### 1. Context sources (collectors)

Each source is a function `(session_state, task) -> list[ContextBlock]`.
A block carries:

- `kind`: system | task | memory | tool_result | retrieved | history
- `content`: the text
- `priority`: float (higher = more important to keep)
- `token_estimate`: approximate size
- `source_id`: stable key for dedup and audit

Built-in sources:

| Source | What it collects | When it fires |
|--------|------------------|---------------|
| `system` | The agent's declared instructions | Always |
| `task` | Current task spec, acceptance criteria | Always |
| `memory` | Relevant memory entries (scored) | If memory has entries |
| `history` | Prior turn summaries (compacted) | If history exceeds window |
| `tools` | Recent tool results (summarized) | After tool use |
| `retrieval` | Docs retrieved for this step | If a retrieval tool fired |

Sources are declared on the agent, not wired in the runner. The runner
discovers them from the agent spec and invokes them in declared order.

### 2. Policy gates

Before blocks enter the prompt, a **policy** decides which are allowed.
This is the governance surface — the same Unity Catalog / permission model
that gates tools also gates context:

- **Allowlist**: only these `source_id`s may appear.
- **Redaction**: PII patterns stripped from `content` before inclusion.
- **Phase gate**: certain sources only fire in certain loop phases
  (e.g. retrieval sources suppressed during the "decide" phase).

A block that fails policy is dropped with an audit record, not silently
omitted.

### 3. Budget allocator

The compiled prompt must fit the model's context window minus reserved
output tokens. The allocator:

1. Sums `token_estimate` across surviving blocks.
2. If over budget, drops lowest-`priority` blocks until it fits.
3. Never drops `system` or `task` blocks (they are pinned).
4. Emits a `context_budget` event: blocks kept, blocks dropped, tokens used.

### 4. Compilation

Surviving blocks are ordered by a declared **layout** (default:
system, task, memory, history, tools, retrieval) and joined into the
final prompt string. The layout is part of the agent spec so two agents
can compile different shapes from the same sources.

## What this replaces

| Today (scaffolded) | Context engine |
|--------------------|----------------|
| One template per step | Dynamic compilation per step |
| Fixed slots | Modular sources |
| No governance on prompt content | Policy gates with audit |
| No token budgeting | Priority-based eviction |
| Template is code | Layout is declared config |

The workflow engine (`discovery/workflow.py`) keeps its durable step
execution. The context engine sits *inside* each step's handler, replacing
the `{{template}}` render with a compilation pass.

## Where it lives

```
python/src/apx_agent/context/
    __init__.py          # public surface: compile_prompt()
    sources.py           # ContextSource protocol + built-in sources
    policy.py            # PolicyGate protocol + allow/redact/phase gates
    budget.py            # BudgetAllocator
    compiler.py          # compile() — the pipeline
    blocks.py            # ContextBlock dataclass
```

The workflow handler calls `compile_prompt(agent_spec, session_state)`
instead of rendering a template. The compiled string is what gets sent to
the model.

## Declaration surface

An agent spec gains an optional `context` block:

```yaml
context:
  sources: [system, task, memory, history, tools]
  policy:
    allow: [system, task, memory]
    redact: ["ssn", "email"]
  budget:
    max_tokens: 8000
    pin: [system, task]
  layout: [system, task, memory, history, tools]
```

Declared, not wired — the compiler reads this and builds the pipeline.
No runner code changes when a new source or gate is added; it is
registered and declared.

## Non-goals (this design)

- **Learning which sources matter.** A future optimizer could score
  source utility from eval results and adjust priorities. Not in scope.
- **Multi-model compilation.** One compiled prompt per step. Routing
  different block subsets to different models is a later concern.
- **Streaming compilation.** The prompt is compiled before the call,
  not incrementally during generation.

## Acceptance

- A declared `context` block compiles to a prompt whose blocks match
  the declaration (source order, policy drops, budget eviction).
- Dropped blocks emit an audit event with the reason (policy or budget).
- Pinned blocks (`system`, `task`) are never evicted.
- A workflow step handler that swaps template rendering for
  `compile_prompt()` produces a valid model call.
- Claim-vs-reality: a test reads the compiled prompt back and asserts
  the block sequence, not just that compilation returned a string.
