# Disambiguating `publish` — split by object

**Date:** 2026-06-13
**Status:** Implemented. (Correction during build: the two tools-registry writes are NOT duplicates — `uc publish` catalogs *standalone* tools, `advertise` catalogs *agent-attributed* tools — so `uc publish` is left unchanged; the work is pure command split/grouping.)
**Location:** `src/apx_agent/cli.py` (command surface only; underlying `_publish.py` functions unchanged)

## Problem

`publish` is overloaded across two commands that also duplicate work:

- **`apx uc publish`** — (1) publishes `@tool(uc=…)` tools as **executable UC functions**, and (2) writes those tools to the **tools registry** table (`main.apx.agent_tools`).
- **`apx agents publish`** (docstring: "advertise this agent to the organisation") — (1) writes the **agent** to the discovery registry (`main.apx.agent_registry`), (2) writes its tools to the **same tools registry** (`main.apx.agent_tools`) — *duplicating `uc publish`* — and (3) registers the agent with a **Mosaic Supervisor** when `supervisor_id` is set.

So one verb spans four objects (tools-as-functions, tools-as-discovery-rows, agent-as-discovery-row, agent-into-supervisor), the tools-registry write happens in two places, and a *third* supervisor command (`agents create-supervisor`) sits separately.

## Design — one command per object/intent

| Intent | Command | Does |
|---|---|---|
| **Executable** | `apx uc publish` | Publish `@tool(uc=…)` as UC functions + catalog them as **standalone** tool rows. **Unchanged.** |
| **Discoverable** | `apx agents advertise` *(new)* | Write the agent → `agent_registry` and its **agent-attributed** tools → `agent_tools`. |
| **Routable** | `apx supervisor create` *(new)* | Create a Mosaic Supervisor (= today's `agents create-supervisor`). |
| **Routable** | `apx supervisor add` *(new)* | Register a deployed agent endpoint as a Supervisor sub-agent (= the supervisor part of today's `agents publish`). |

Naming decisions (settled): **`advertise`** not `register` (— "register" already means *UC model-version registration* in the deploy path; reusing it re-introduces the overload). `uc publish` stays under the `uc` group (no new `tools` group) to minimize churn.

### On the tools-registry write — NOT a true duplicate (corrected)

Initial read called the tools-registry write duplicated. On inspection it is **two distinct writes** to the same `agent_tools` table:

- `uc publish` → `publish_standalone_tools_to_registry(tool_fns, tools_table, ws)` — tools cataloged **independent of any agent** (discoverable on their own).
- `agents publish` → `publish_tools_to_registry(agent_id, agent_name, tool_fns, …)` — tools **attributed to a specific agent**.

Different rows, different discovery purpose. So **`uc publish` is left UNCHANGED** (no lost capability), and `agents advertise` owns the agent + agent-attributed-tools writes. The disambiguation is therefore purely **command splitting/grouping** — no behavior change to `uc publish`, and the only behavior move is `agents publish` → (`advertise` + `supervisor add`).

## Backward compatibility — nothing breaks

Old commands stay, print a one-line deprecation notice, and delegate:

- **`apx agents publish`** → runs `advertise`, then `supervisor add` when `supervisor_id` is configured (preserves the one-shot behavior). Notice: "`agents publish` is deprecated; use `agents advertise` (+ `supervisor add`)."
- **`apx agents create-supervisor`** → `supervisor create`. Notice points to the new command.
- **`uc publish`** — unchanged (no deprecation; it already reads as "publish tools to UC").

Deprecated aliases keep all their current flags so existing scripts/pyproject config keep working.

## Components

- `cli.py`: new `supervisor` group (`create`, `add`); new `agents advertise`; rewrite `agents publish` / `agents create-supervisor` as thin deprecating delegators. `uc publish` untouched.
- `_publish.py`: unchanged — `publish_tools_to_uc`, `publish_standalone_tools_to_registry`, `publish_to_supervisor`, `create_supervisor_agent` are reused by the reorganized commands.
- Config keys (`[tool.apx.agent].supervisor_id`, `registry_table`, `tools_table`) keep their meanings; `advertise` / `supervisor add` read the same ones.

## Testing

- New: `agents advertise` writes the agent registry + agent-attributed tools; `supervisor create` / `supervisor add` call the right `_publish.py` entry points.
- Back-compat: `agents publish` still advertises (+ supervisor when configured) and prints the deprecation notice; `agents create-supervisor` delegates to `supervisor create`.
- Update existing `uc publish` / `agents publish` / `create-supervisor` tests to the new behavior (convert, don't weaken; delete only where a subject genuinely moved and is covered elsewhere).

## Out of scope

- No change to `_publish.py` logic or the registry table schemas.
- No change to `deploy` / `canary` / `hot-swap`.
- A `tools` noun group (instead of `uc publish`) — considered and declined for churn.
