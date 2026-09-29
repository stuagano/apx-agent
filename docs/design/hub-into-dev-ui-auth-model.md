# Design: fold the hub into the `_apx` dev-UI, with one auth model

**Status:** brainstorming / design draft · **Date:** 2026-09-28

The deployable hub (`hub/`) and the served dev-UI (`_apx/*` in `_dev.py`) both
answer "what agents are in this workspace, and let me work with them." They grew
separately, so they have **two different auth models** — and the hub's is a
patchwork, including one route that leaks under the app service principal. The
`_apx` surface, by contrast, was hardened into a clean caller-scoped, fail-closed
model (#610–#629).

**Decision (from review):** make the hub a built-in part of the dev experience by
**folding it into the `_apx` dev-UI**, and give it the **same auth model `_apx`
already has** — caller-OBO, fail-closed. Consolidate onto the good model rather
than maintain two.

---

## Current state (verified against source)

**`_apx` dev-UI — the good model.** Discovery routes call `_ws_prefer_obo(request)`
(`_defaults.py:189`), which runs as the **calling user's OBO token** and **raises
401 on a deployed App when OBO is missing** (`_defaults.py:213`) — never silently
lists under the app SP. Mutations that change the shared live agent additionally
require an operator secret (`APX_DEV_UI_TOKEN`, HMAC) + an SSRF/Apps-host
allowlist. `/_apx/workspace-agents` (`_dev.py:3417`) already lists app + UC agents
under this model — **this is the hub's core feature, already done right.**

**Hub — the patchwork.** (`hub/src/agent_hub/backend/router.py`)
- `/current-user`, `/agents/register` → `Dependencies.UserClient` (**OBO** — good).
- `/agents/discover-workspace` → bare `WorkspaceClient()` (`router.py:276`) —
  **app SP, workspace-wide.** The leak: lists under SP grants, not the caller's.
- `/agents/{id}/invoke` → reads `X-Forwarded-Access-Token` by hand
  (`router.py:324`) — OBO-ish, but not the `_ws_prefer_obo` fail-closed path.
- Startup `_auto_register` / lifespan → `WorkspaceClient()` app SP (`app.py:44`).

The hub already depends on apx-agent (`from apx_agent import Dependencies`,
`from apx_agent._apps_discovery import discover_app_agents`) — so the shared
primitives are already reachable; they're just used inconsistently.

---

## The auth model (target)

One model for the whole dev surface, hub included:

1. **Read / discovery = caller-OBO, fail-closed.** Every "list/see other agents"
   path resolves identity via `_ws_prefer_obo` and returns only what the calling
   user can see. On a deployed App with no OBO → **401**, never an app-SP listing.
   This is the resolution of the visibility-vs-scope tension: the hub shows *your*
   agents (the ones you have grants on), not an SP-wide recon of the workspace.
   Consequence to state plainly: the hub is **"the agents you can see,"** not "every
   agent that exists." That is the correct default for a governed platform.
2. **Mutations = OBO + operator gate.** register / deregister / wire / refresh that
   change shared state reuse the existing `APX_DEV_UI_TOKEN` HMAC gate + SSRF
   allowlist. No new mechanism.
3. **Invoke = caller-OBO.** Invoking an agent runs as the user (their grants gate
   the downstream), via `_ws_prefer_obo`, not a hand-rolled header read.
4. **Bootstrap SP use is the one allowed exception, and must be explicit.** A hub
   that wants a workspace-wide seed at startup (no user in scope yet) may use the
   app SP — but only behind the existing `APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK`
   opt-in (+ warning), the same escape hatch `_apx` already defines. Default off.

Net: the hub inherits `_apx`'s hardening instead of re-deriving it — and the
`discover-workspace` leak closes by construction (it becomes `_ws_prefer_obo`).

---

## The fold

`/_apx/workspace-agents` already covers discovery. What the hub adds on top, and
where it lands in the dev-UI:

| Hub surface | Fold target |
|---|---|
| `discover-workspace` (SP-wide) | **delete** — `/_apx/workspace-agents` already does this, caller-OBO |
| `register` / `deregister` / `refresh` | `_apx` mutation routes under the existing operator gate |
| `invoke` | `_apx` invoke under `_ws_prefer_obo` |
| AgentCard model (`models.py`), UI | port into the dev-UI's view layer |
| startup `_auto_register` | optional SP-seed behind the opt-in flag |

Result: **one served dev surface, one auth model, one set of SSRF/dev-token/OBO
plumbing** — the hub becomes a view in the dev-UI, not a second app with its own
(weaker) identity story.

---

## Tradeoffs / what we accept

- **Hub view becomes caller-scoped.** A user who could previously see the full
  SP-wide list now sees only their own visible agents. This is the point (no more
  SP recon), but it changes the hub's "feel" from "workspace map" to "my agents."
  The SP-wide map is still available to an operator via the explicit opt-in.
- **Consolidation cost.** Folding is more work than patching the one leaky route.
  The payoff is not maintaining two auth models — the thing that produced the leak
  in the first place.

## Open questions (for the plan)

1. **Does the hub stay separately deployable at all**, or is it *only* a dev-UI
   view? (Review picked "fold into `_apx`"; confirm whether `hub/` the deployable
   is retired or kept as a thin wrapper around the dev-UI surface.)
2. **Registry-backed vs live discovery.** A future option (not this pass): list
   from the UC `agent_registry` table (governed by that table's grants) for a
   fleet view that is neither SP-recon nor limited to `apps.list` — deferred.
3. **UI port scope.** How much of the hub UI moves vs. is rebuilt in the dev-UI's
   idiom.

---

See also:
[`served-path-guards-and-identity.md`](served-path-guards-and-identity.md) (the
`_apx` identity model this adopts),
[`agent-governance-for-uc-data-architectures.md`](agent-governance-for-uc-data-architectures.md)
(the identity principles),
[`day-2-upgrades-and-versioning.md`](day-2-upgrades-and-versioning.md).
