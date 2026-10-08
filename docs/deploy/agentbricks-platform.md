# How the Agent Bricks platform works (and where APX fits)

APX deploys agents onto **Agent Bricks**, Databricks' managed product for custom
agents. Most of what a deployed APX agent *does* at runtime — stay durable,
scale to zero, run tools under an identity — is Agent Bricks behavior, not APX
behavior. This page explains the product first, from the ground up, then shows
where APX adds value on top. Read it once and the rest of the deploy docs make
sense.

The canonical product reference is
[`databricks/databricks-ai-bridge` › `integrations/agentbricks`](https://github.com/databricks/databricks-ai-bridge/tree/main/integrations/agentbricks)
(README + `cli.md`). Where this page and that reference could drift, upstream is
the source of truth for the API, the CLI, and the stores; APX owns the
declaration and policy layered on top.

---

## 1. How a durable agent works

A durable agent is **a worker plus a database.** The worker runs your agent
code. The database — the managed **Runtime Store** — holds the record of every
job. The worker is disposable; the database is not. That split is the whole
idea: because the record of what's happening lives in the database and not in
the worker's memory, the worker can be stopped, killed, and replaced without
losing anything.

(There are actually three managed stores, each with its own job: the **Runtime
Store** keeps the record of invocations — requests, status, heartbeats, events,
results; the **Session Store** keeps conversation history; the **Memory Store**
keeps long-term facts recalled across conversations. For the rest of this
section "the database" means the Runtime Store, the one recovery depends on.)

Three things can happen to the worker:

**1. Nobody's using it → it goes to sleep (scale to zero).** With no traffic for
a while, Databricks shuts the worker off so you stop paying for idle compute.
The database is untouched. The next request starts a fresh worker, which reads
the database back and carries on — you pay a few seconds of cold start and lose
nothing. This is automatic and is the normal resting state. (Exactly which
deployments get this is a tier choice — see §3.)

**2. The worker dies mid-job → the job isn't lost.** If a worker crashes, OOMs,
or is redeployed while running an invocation, the job's progress is already in
the database, so a new worker can pick it up and finish it. That is what
"durable" means. (This is opt-in: enable it with `@app.recover` /
`RuntimeRequirements(recovery=True)`, service-identity agents only.)

**3. The catch: who notices an abandoned job and restarts it?** A *living
worker* does. The "scan for abandoned jobs and restart them" routine runs
**inside a worker** (a running job refreshes a heartbeat; a scan loop in the
worker watches for heartbeats that have gone stale). So *when* a stale job gets
restarted depends on whether another worker is alive — which is decided by the
hosting mode (§3; the modes appear to be mutually exclusive — see the inference
note there):

- **Scale-to-zero tier (0↔1 instance)** — the default. Never more than one
  worker, so if it dies there is nobody left to notice. The job waits safely in
  the database until a worker exists again (next request's cold start, or a
  redeploy), and *that* worker's scan loop finishes it. For a scale-to-zero
  agent this "waits for a worker" is the normal case, not an edge case.
- **Dedicated-instance tier (multiple always-on workers)** — if one dies, a
  surviving worker's scan loop restarts the job right away. Instant recovery,
  but this tier does **not** scale to zero.

**Either way the job is never lost — the only difference is how soon a worker is
around to pick it up.**

Two limits worth knowing: a **request-user** (`auth = "user"`) job can't be
recovered — the forwarded caller credential is deliberately never written to the
database (§4), so a restart fails fast with `MCP_USER_AUTH_RECOVERY_UNSUPPORTED`
before your code runs. And recovery is **at-least-once**: a restarted job may
re-run external side effects, so tools must be idempotent (safe to run twice).

---

## 2. The CLI, end to end

One CLI (`agentbricks`) takes you from an empty directory to a deployed,
governed agent. The agent lives in a local project folder, and a file called
**`agent.toml`** records everything about it — name, framework, server type,
declared stores, tools, tracing. The commands all read and write that project.

The happy path is five commands:

```sh
agentbricks login --profile <profile>   # 1. auth: save a default profile
agentbricks init my-agent                # 2. scaffold a project (+ agent.toml)
cd my-agent
agentbricks dev                          # 3. run locally, chat UI at :8000
agentbricks deploy my-agent              # 4. deploy to Databricks Apps
agentbricks deployments get agent-bricks-my-agent   # 5. URL + status
```

| Step | Command | What it does |
|---|---|---|
| Auth | `login` | Saves a default Databricks profile so later commands don't need `--profile`. |
| Scaffold | `init` | Creates the project + `agent.toml`. Picks the **framework** and **server type** (below) and declares default memory + session stores. |
| Run locally | `dev` | Runs on `localhost:8000` wrapping the real Apps local runtime, so local == deployed behavior. Traces locally under `.agentbricks/`. |
| Deploy | `deploy` | Stands up an App named `agent-bricks-<name>`, reconciles stores and tool access, wires tracing, prints a URL (§3). |
| Operate | `deployments` | `list` / `get` / `logs` / `start` / `stop` / `delete` deployed apps. |

**Two choices are baked in at `init`, not `deploy`** — because they shape the
scaffolded code, not just the runtime:

- **`--framework langgraph | openai`** — which agent framework your code uses.
- **`--server agentbricks | custom`**:
  - `agentbricks` (default) = the managed server = `DurableAgentServer`: the
    Runtime Store, the invocation API, streaming/background/recovery — everything
    in §1.
  - `custom` = a minimal foreground-only FastAPI server, no Runtime Store.
  - You **cannot change this on a live deployment** — switching means scaffolding
    a new project.

Supporting command groups edit `agent.toml`: `memory` / `sessions` (stores,
entries, items; sessions can `fork`), `tools` (`add sandbox|mcp|uc-function|genie-one|genie-agent`,
§4), `tracing` (bind/inspect the MLflow experiment, on by default), `mcp`
(discover managed MCP services), `endpoint` (fire an HTTP request to exercise
the agent), `doctor` (check onboarding).

**Mental model:** `agent.toml` is the source of truth; the command groups edit
it; `deploy` reconciles reality to it (creates missing stores, grants the SP,
wires tracing) but never edits `agent.toml` for stores.

---

## 3. `deploy`, and the two hosting tiers

```sh
agentbricks deploy [NAME] [--source .] [--instances N] [--allow-user-scope-update] \
                   [--pip-index-url ...] [--workspace-path ...]
```

`deploy` turns the project into a running App named `agent-bricks-<name>`. In
order it: reconciles declared **stores** (creates missing, grants the SP);
reconciles **tool access** (§4); wires **tracing**; uploads source and starts
the app, reaching model serving **through the AI Gateway as the app's own
service principal** — no model keys; and prints a **URL**.

The options are few on purpose — only things safe to change on a live app.
Server type and tools are *not* deploy flags; they live in `agent.toml`.

**Fail-closed gate:** if any required direct grant cannot be read, applied, or
verified, deploy stops *before* source upload and leaves the currently deployed
version untouched. It won't ship a half-granted app.

### Hosting: App Space, scale-to-zero, and horizontal scaling

Three terms get conflated here; keep them separate.

**An App Space is a governance boundary, not a compute tier.** Per Databricks,
an App Space *"defines who can create apps, how apps can be shared, and what
permissions apps have."* It's a container for *who / how / permissions* — it says
nothing about how many workers run. Don't call the compute "the App Space
runtime": the space governs, the runtime computes, and they're separate
concerns.

**Scale-to-zero is a property of the serverless app runtime.** Each serverless
app *"scales between zero and one instance and scales down after a default 30
minutes of idle time,"* and *"state in memory or on local disk is lost when the
app scales down"* — which is exactly why durable state lives in the external
stores (§1), not in the worker. The first request after scale-down pays a short
cold start. There is no `--scale-to-zero` switch; it is simply what the
serverless runtime does. The shape to remember: **0↔1 means at most one worker.**

**Horizontal scaling is the other mode.** A horizontally scaled app runs a
**fixed number of instances, 1–5** (*"Each horizontally scaled app can have at
most 5 instances"*; Databricks recommends at least 2 for availability), set as a
static count — this is what `--instances N` (and APX's declared `instances`)
drives. Multiple always-on workers sit behind one URL with best-effort sticky
routing.

### `LIQUID` and the App Space tie-in

Scale-to-zero rides on a compute size called **`LIQUID`**, and the surprising
part — verified against the live API, not inferred — is that **you cannot select
`LIQUID`. You get it by putting the app in an App Space.** The evidence:

- `databricks apps create` exposes both `--space` and `--compute-size`, but
  `--compute-size` accepts only **`MEDIUM | LARGE | XLARGE`**. `LIQUID` is not an
  acceptable value — there is no way to *ask* for it.
- A plain deploy with **no space** lands on **`MEDIUM`**, a fixed size, with no
  scale-to-zero. (Verified: a vanilla `agentbricks deploy` with no `--instances`
  and no space produced `space: null`, `compute_size: MEDIUM`.)
- An app **in an App Space** runs on **`LIQUID`** and scales to zero. The space
  payload itself carries *no* compute field — so `LIQUID` is not stored on the
  space; **membership in the space is what switches the app onto the serverless
  runtime** at create time.

So the tie-in is real and a little odd: an App Space is a *governance* object
(who-can-create, scopes, usage policy — no compute setting on it), yet whether
an app belongs to one is the de-facto toggle between the two compute models:

| | No space | In an App Space |
|---|---|---|
| `compute_size` | `MEDIUM`/`LARGE`/`XLARGE` (you pick; default `MEDIUM`) | `LIQUID` (imposed; not selectable) |
| Scaling | fixed instances, `--instances 1–5`, always-on | serverless 0↔1, scales to zero |
| Set by | `apps create --compute-size` / `--instances` | `apps create --space`, or Genie App Builder, or APX's space path |

This is why APX's App-Space deploy path *rejects* any `compute_size` /
`--instances` in your declaration (*"App Space selects its compute"*,
`_app_space.py`) and only verifies `compute_size == "LIQUID"` on readback — the
space owns the compute decision, so declaring one would be a contradiction. The
installed Databricks SDK's `ComputeSize` enum predates `LIQUID` and deserializes
it as `None`, so APX reads it from the raw REST payload.

**An App Space is a prerequisite you set up first — it is not created by a
deploy.** It is a standalone governed resource (`databricks apps list-spaces` /
`get-space`), and the **Governed agentic app-building** feature is **Beta**: a
workspace admin must enable it from the **Previews** page before spaces can be
created. Create an app into an existing space with `apps create --space <name>`,
through Genie App Builder (the natural-language app UI, Build tab → pick a
space), or through APX's declared-space path. None of these *create* the space;
they deploy into one that already exists.

#### Deploying into a space: what worked vs. what didn't (observed on fevm)

Inspecting the App-Space apps on this workspace shows a clear split, and a
gotcha worth calling out:

| App | Created by | Deployment | Launched? |
|---|---|---|---|
| `stu-appspace-probe-1002`, `stu-appspace-control-1002` | updater `apps+gateway+private+preview` | `status: SUCCEEDED` ("Traffic switched") | **yes** |
| `agent-card-aggregator` | `stuart.gano` directly | **no deployments at all** | **no** — created in the space, never ran (logs 502, nothing serving) |

The apps that launched were deployed through the governed-agentic/preview path
(the preview service principal is the updater) and have a SUCCEEDED deployment.
The one that didn't was created directly into the space but **never had a source
deployment** — the app shell existed with nothing to run.

The lesson: **putting an app in a space is two steps, and the space-membership
step alone doesn't launch anything.** `apps create --space` makes the app
resource (on `LIQUID`); you still need a successful *deployment* of source onto
it. A manual `apps create --space` that stops there leaves exactly this state —
an app in the space, on LIQUID, that never launches. This is part of what APX's
space path does for you: it validates the space up front
(`validate_space_deployment` checks `effective_user_api_scopes` and required
service resources against the agent's needs, failing early with a named reason),
drives the deployment, and then binds the Runtime Store
(`runtime_store_env`) — so you don't end up with a created-but-never-launched
shell. See `_app_space.py`.

Not fully pinned (honest): the space here (`app-space`) carries a complete scope
set (`sql`, `genie`, `postgres`, `model-serving`, `vector-search`, `mcp.*`, …),
so the stalled app looks more like an incomplete two-step than a permissions
denial — but a space missing scopes/resources *would* also fail, and APX's
pre-flight is what turns that into an early, legible error instead of a silent
non-launch.

Honest limits still open:

- Public docs don't define what `LIQUID` *is* beyond the App-Space compute size;
  the "elastic/serverless size" reading is inference, and it currently surfaces
  through a private-preview path, so don't treat `LIQUID` as a stable public
  `compute_size` value.
- The docs don't state in one sentence that a horizontally scaled (fixed-instance)
  app *can't also* scale to zero, but the create API makes it concrete: fixed
  size + `--instances` is one shape, `LIQUID`-via-space is the other, and
  `--compute-size` can't be `LIQUID` — so you pick one model, not both.

This tier split is also what §1's recovery timing hinges on: a scale-to-zero
(App-Space, 0↔1) app is single-worker — a crashed job waits for the next worker;
a fixed-instance app with ≥2 workers has a surviving worker to recover instantly
but stays warm.

Sources: live `databricks apps create` / `list-spaces` / `get-space` on this
workspace; Databricks
[governed-agentic app building](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/governed-agentic-app-building)
(App Space, Genie App Builder, Beta/Previews enablement) and
[horizontal scaling](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/horizontal-scaling)
(the 1–5 limit); and `_app_space.py` for APX's space-deploy handling.

---

## 4. Identity: two identities, chosen per tool

This is the heart of the governance model. Every tool runs as **one of two
identities**, set by `--auth` when you add it and recorded on the tool entry in
`agent.toml`:

```toml
[[tools]]
id = "web_search"
auth = "user"                                   # or "app"
source = { kind = "mcp", service = "system.ai.web_search" }
```

|  | `auth = "app"` | `auth = "user"` |
|---|---|---|
| Runs as | the App's **service principal** | the **calling user** (OBO — their credential forwarded) |
| Permissions | what the app SP is granted | what the end user is allowed |
| Default? | — | **yes**, for managed tools (`mcp`, `sandbox`, `genie-one`, `genie-agent`) |
| Deploy grants access? | **yes** — auto least-privilege (below) | **no** — skipped; the user's own permissions apply at call time |

**What deploy auto-grants for `auth = "app"` tools** (least-privilege, *direct*
resources only):

| Declared resource | SP gets |
|---|---|
| UC function | `FUNCTION` / `EXECUTE` |
| Genie Agent space | `CAN_RUN` |
| Sandbox volume | `READ_VOLUME` / `WRITE_VOLUME` |
| External MCP | effective `EXECUTE` + `USE_SCHEMA` + `USE_CATALOG` on named parents |

Sharp edges:

- **Only direct resources.** Deploy does **not** grant the tables a Genie Space
  reads, objects a UC function calls, or what an MCP service wraps — grant those
  transitive dependencies manually.
- **UC-function bindings are app-identity only** — they don't accept `--auth user`.
- **Missing/legacy `auth` means app identity**, and is never silently upgraded to
  user.

**How user-identity flows at runtime:** `DurableAgentServer` derives its
request-auth policy straight from the `auth = "user"` tool bindings — no separate
contract marker. A code-first tool gets the caller's client from a
**request-bound resolver** inside the invocation and must not persist it:

```python
def sql_tools(workspace_client_for):
    @tool
    def run_statement(statement: str) -> str:
        client = workspace_client_for("user")   # request-bound; closes after the attempt
        ...
    return [run_statement]
```

The forwarded credential is process-local and never written to the Runtime
Store — which is exactly why user-auth work **can't be recovered** after a crash
(`MCP_USER_AUTH_RECOVERY_UNSUPPORTED`, §1).

**Scopes.** Deploy infers the API scopes a user-auth tool needs from its managed
bindings; code-first tools declare extras explicitly:

```toml
[auth.user]
required = true
additional_api_scopes = ["sql"]
```

This is **additive** — deploy unions inferred + declared scopes, dedupes, and
preserves existing ones. Adding *new* scopes to an already-deployed app needs
`--allow-user-scope-update` once (later deploys don't).

**One-sentence model:** deploy reconciles identity — it auto-grants the app SP
least-privilege access for `app`-identity tools, skips `user`-identity tools
(whose access is the caller's own, forwarded per-request via OBO), unions the
required scopes, and refuses to ship if any grant can't be verified.

---

## 5. Where APX fits

Everything above is the Agent Bricks product. APX does not replace it — it
**declares** the agent so you write intent instead of wiring, and compiles that
declaration into the inputs the product consumes.

- **One declarative envelope.** A single `[tool.apx.agent]` block in
  `pyproject.toml` carries name, model, instructions, tools, composition
  (`sub_agents`), memory/session choice, and deploy/scaling — instead of
  hand-maintaining `agent.toml`, store bindings, and bundle files separately.
  Generated manifests are compiler output, not config you keep in sync. See
  [`reference/pyproject-toml.md`](../reference/pyproject-toml.md).
- **Governance and identity wiring.** Per-tool identity and UC/secret ceilings
  (tool-scoped auth), caller-identity passthrough (OBO) across tools and across
  agents (A2A), and guardrails that refuse unsafe combinations — e.g. a scaled
  (dedicated-tier) deployment with an in-memory session store is rejected at
  compile and at boot, because that silently loses history across workers (see
  [`running/sessions-and-memory.md`](../running/sessions-and-memory.md)).
- **Compatibility checking before you deploy.** `inspect_target` / `compile_agent`
  check whether the chosen runtime can preserve what you declared (identity,
  sessions, approvals, memory, streaming, recovery) and fail with a named reason
  instead of silently degrading. See
  [`running/runtime-targets.md`](../running/runtime-targets.md).
- **One command that sequences a multi-step, multi-store setup.** Standing up a
  durable agent in an App Space is not one action — it is a create → bind →
  redeploy dance that touches *three* managed stores, and the raw CLI leaves the
  sequencing to you (stop early and you get a created-but-never-launched shell —
  see §3). APX's single `deploy` does it in order:
  1. **Pre-flight everything that must already exist** — the App Space and each
     declared store are probed before anything is built, and a missing /
     feature-disabled / permission-denied resource fails fast with a named
     reason instead of a half-made app (`_doctor.py` `check_agent_prerequisites`).
  2. **Create the app** (first `bundle deploy`) so its service principal exists.
  3. **Bind the Runtime Store** against that just-created SP, then **redeploy
     with it wired in** (second `bundle deploy`) — the deploy that runs durably.

  **APX never implements store creation itself** — the actual `.create()` for all
  three stores lives in the Agent Bricks / AgentKit SDK. What differs is how APX
  *triggers* it, and the three are **not** symmetric:

  | Store | Who runs the create | APX's role |
  |---|---|---|
  | **Runtime** | Agent Bricks `get_or_create_backend` | calls it **inline during deploy** (after the SP exists), reads back `space` + `LIQUID`, wires the `DATABRICKS_AGENTBRICKS_RUNTIME_STORE_*` env (`_app_space.py` `runtime_store_env`) |
  | **Memory** | AgentKit `memory_stores.create` | has its **own provisioning wrapper + CLI** (`provision_managed_memory`, create-only-on-404-never-on-permission; `apx-agent memory …`) |
  | **Session** | AgentKit `session_stores` / `DatabricksSessionStoreSaver` | **declares and binds only** — writes `session_store` into the generated config and the `AGENT_SESSION_STORE` env, and pre-flights it (step 1); no APX create path |

  The Memory-vs-Session difference is **intentional, and it tracks the store's
  lifetime, not an inconsistency.** A memory store is meant to be *shared across
  many agents* (provision once, every agent binds the same `store_name` to
  accumulate common institutional memory), so it has a standalone provisioning
  path that exists outside any single deploy. A session store belongs to one
  agent's conversations, so it has no reason to exist before that agent — declare
  it and let the deploy provision it. Shared lifetime → independent provisioning
  (Memory); per-agent lifetime → deploy-time provisioning (Session).

  So APX's contribution is **orchestration, not provisioning**: it collapses the
  chicken-and-egg ordering (the Runtime Store is owned by an SP that doesn't
  exist until the app is created) and the per-store reconciliation into one
  command, calling the SDK's own create at the right moment and failing fast when
  a prerequisite is missing. See
  [`running/sessions-and-memory.md`](../running/sessions-and-memory.md) for the
  stores themselves.
- **Portability.** The same declaration compiles to the durable Apps target or to
  a `ResponsesAgent` model for Model Serving, so moving between serving contracts
  is a target switch, not a rewrite.

**Rule of thumb:** the CLI gives you the running, durable, scale-to-zero app;
APX gives you the declaration, governance, and safety checks that make a fleet
of them maintainable.

For a lifecycle actually observed on a live deployment (idle → wake → crash →
replay), see the "Observed deployed lifecycle" note in
[`running/runtime-targets.md`](../running/runtime-targets.md).
