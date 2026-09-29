# Day-2: Upgrades, Versions, and Rollback

**Status:** reference · **Date:** 2026-09-28

Deploying an agent once is day-1. The day-2 story is everything after: how you
ship a new version, prove it's better (or safer), promote it, and roll it back
when it isn't. This document is the honest account of what apx-agent gives you
today — including where the two deploy targets differ, because that difference is
the single most important thing to understand before you rely on it.

The short version:

- **Model Serving target** → real blue/green. Versioned artifacts, platform
  traffic-split canary, **instant** rollback.
- **Apps target** → disciplined redeploy. No platform traffic-split; a "version"
  is a stamped label, canary is a side-by-side app, rollback is a redeploy of the
  prior label. Honest and observable, but **manual**.

Pick the target knowing which story you're buying.

---

## What a "version" is

It depends on the target, and this is the root of everything below.

**Model Serving.** A version is a first-class artifact: a Unity Catalog
registered-model version behind a serving endpoint. The platform knows v41 and
v42 are distinct entities and can route between them. This is the strong case.

**Apps.** Apps has no registered-model-version concept for the running app. A
version is a **label you stamp** — a git SHA and/or an `APX_MODEL_VERSION` value
threaded into the bundle (`APP_MODEL_VERSION` env → every trace the app emits
carries `apx.model_version` / `apx.git_sha`). The platform does not track it for
you; the label is only as good as your discipline in setting it. Traces are
version-keyed *if* the label is present — otherwise per-version analysis falls
back to workspace-wide ("version=unknown").

**Deploy state.** Both targets record what's deployed in an `ApxAppDeployState`
stored in **workspace** storage (never local — deliberately, to avoid stale/fork
drift), with audit entries (who deployed, when). This is the source of truth for
"what is live and who put it there," and backs drift detection against the
declared config.

---

## The upgrade cycle

### 1. Change

Two kinds of change, and they are not the same day-2 event:

- **Code change** — a tool's implementation, a dependency. Produces a new
  artifact / bundle.
- **Declaration change** — a `[tool.apx.agent]` knob (model, instructions, scopes,
  sub-agents). Some knobs reach the served agent, some are declared-but-not-wired
  — so "what actually changed between v41 and v42" is not always the whole diff.
  Know which knobs are load-bearing before you treat a config edit as a no-op.

### 2. Ship a canary

**Model Serving** (`apx-agent ... canary deploy`): add the new version as a
second served entity at a chosen traffic percentage; the rest is redistributed
across the existing entity. Both versions run concurrently behind one endpoint,
the platform splits traffic. This is a true canary.

**Apps** (`canary deploy` on the Apps target): writes a `canary-<version>` DAB
target and deploys it as a **separate app** (`<prod>-canary-<version>`). There is
**no platform traffic split** — the `--traffic` value is a *hint recorded for the
report*, not a routing directive. You (or a fronting router) decide who hits the
canary. The canary is a real second deployment you compare against prod.

### 3. Analyze

`canary status` shows the live served entities + traffic split (Serving) or the
deployed canary/prod apps (Apps). `canary analyze` compares versions over a
lookback window: per-version request counts, latency P50/P95, and error counts,
from MLflow traces. On both targets this is **version-keyed only if versions are
tagged** — the Apps caveat above applies. "Did v42 actually do better than v41"
is answerable on either target, provided the labels are there.

### 4. Promote

`canary promote <version>`:

- **Model Serving** — send 100% of traffic to the version. Crucially, the other
  served entities **stay configured**, so a rollback can swap traffic back without
  re-adding them. Promotion is a traffic change, not a redeploy.
- **Apps** — promote the canary to prod (redeploy prod at the promoted label). A
  redeploy, not a traffic swap.

### 5. Rollback

This is where the targets diverge most:

- **Model Serving** — `canary rollback <prior-version>` sends 100% back to the
  previous version. Because the prior entity was never torn down, this is
  **instant** and requires no rebuild. This is the tight story.
- **Apps** — rollback is a **redeploy of the previous label**. It is as fast as a
  deploy (not instant) and as reliable as your version-labeling discipline. If
  you didn't stamp the prior version, you're rolling back to "whatever the prior
  bundle was," which is weaker.

---

## Fleet: day-2 across many agents

`apx-agent fleet` applies day-2 operations across a selector of agents rather than
one at a time: `list`, `tag`, `backfill`, `repoint`. Bulk operations default to
dry-run. When you run a hundred agents, "upgrade the fleet" and "who is on which
version" become the operations that matter, and they bind to the `--profile`'s UC
registry so writes land where you expect.

`agents status` is the post-deploy health check ("is it up, what's running?");
`agents register` records the agent; `agents list` inventories them.

---

## The honest matrix

| Concern | Model Serving | Apps |
|---|---|---|
| Version identity | UC registered-model version (first-class) | stamped label (git SHA / `APX_MODEL_VERSION`) |
| Canary | platform traffic-split, concurrent | side-by-side app, **no traffic split** |
| Promote | traffic → 100% (no redeploy) | redeploy at label |
| Rollback | **instant** (prior entity still warm) | redeploy prior label (manual, discipline-dependent) |
| Per-version metrics | yes (trace-keyed) | yes *if labeled*, else workspace-wide |
| Deploy state / audit | workspace-stored `ApxAppDeployState` | same |

---

## Recommendations for a tight day-2

1. **On Apps, always stamp the version** (`APX_MODEL_VERSION` and/or git SHA).
   Every guarantee downstream — per-version metrics, meaningful rollback —
   depends on it. An unlabeled Apps deploy has a weak day-2 story by construction.
2. **Treat declaration changes as versions too.** A model or instruction change
   in `[tool.apx.agent]` is a new version even when no code changed — label and
   canary it like one.
3. **Choose the target for the day-2 story you need.** If instant, traffic-split
   rollback matters (regulated, high-availability), that's Model Serving. Apps is
   the right call for the runtime and cost profile, with a manual rollback you
   accept knowingly.
4. **Rely on workspace deploy-state, not local.** It's the single source of truth
   and it's already where drift detection reads from.

---

See also:
[`agent-governance-for-uc-data-architectures.md`](agent-governance-for-uc-data-architectures.md)
(who the agent is when it runs) and the deploy surface under
[`../deploy/`](../deploy/).
