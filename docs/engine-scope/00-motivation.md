# Agent-as-Config: Why Coworker and apx-agent Are the Same Idea

*For Erskine — connecting the Coworker workstream to the apx-agent engine.*

---

## The realization

Coworker and `apx-agent` aren't a consumer and a dependency that happen to sit
next to each other. They're **the same idea at two altitudes.**

Coworker's whole premise is: *let a person describe an agent in plain terms —
a role, a personality, some skills, a memory — and get a working, governed,
deployed agent back.* That's a **spec → agent compiler** wearing an HR costume.

apx-agent already has one of those. It's called `DataAgent`:

```python
DataAgent("main", "sales", warehouse_id="abc", genie_space="...", ws=w)
```

One line. It introspects the schema, wires the governed SQL/Genie/UC-function
tools, grounds the instructions in the *actual* tables, attaches governed
resources, and hands back a deployable agent. That is a Coworker — a "Data
Analyst" — minus the portal.

So the interesting question was never "how does Coworker call apx-agent." It's
**"what's the shared primitive underneath both, and can we build it once?"**

The answer is the **Template protocol** (spec'd in
`docs/superpowers/specs/2026-05-29-template-protocol-design.md`). And the
throughline you already spotted — **tools as config** — is the rung that
connects your layer to the engine.

## The ladder

| Level | What | Form | Owner |
|------:|------|------|-------|
| 0 | `LlmAgent` | raw primitive, code | engine |
| 1 | **`DataAgent`** | a domain template — data in → tools + grounded instructions | engine (today) |
| 2 | **`[tool.apx.tools]`** | general data→tools, declarative | engine (scoped) |
| 3 | **Coworker templates + catalog** | named, user-facing roles | **you** |

`DataAgent` lives at Level 1 and proves the pattern works. You want Level 3.
The Template protocol generalizes Level 1 into a registrable abstraction, and
`tools-as-config` is the Level-2 substrate it stands on. **Coworker isn't
bolting a translation layer onto a foreign engine — it's the top rung of a
ladder the engine is already climbing.**

## The dots, connected

Here's the part that should make the bespoke "CoworkerSpec → Python DSL
translation layer" from the original architecture doc feel like *much* less
work than feared.

**1. A "Data Analyst coworker" already exists.** It's `DataAgent`. Your catalog
ships it as a template entry, not as new code.

**2. Your templates plug into a shared registry — from your own repo.** Via a
Python entry point, no fork, no import gymnastics:

```toml
# coworker/pyproject.toml
[project.entry-points."apx_agent.templates"]
meeting_manager = "coworker.templates:MeetingTemplate"
personal_assistant = "coworker.templates:AssistantTemplate"
```

```python
registry.list()                       # -> your catalog UI, for free
registry.build("data", spec, ws=w)     # -> a wired, governed agent
```

`registry.list()` returns each template's name, title, description, **and its
JSON schema** — so your catalog and your spec-editor UI render themselves from
the engine's metadata. You don't hand-maintain a parallel schema.

**3. The HR metaphor maps onto a clean technical seam.** This fell out of the
design naturally:

- **Template = role.** "Data Analyst", "Meeting Manager". Skills + grounding.
- **Envelope = persona.** Which model (≈ *education*), instruction tone
  (≈ *personality*), generation knobs. This is the existing
  `[tool.apx.agent]` config, layered onto the built agent automatically.

So a CoworkerSpec splits exactly along the seam the engine already has:

```yaml
role: "Data Analyst"        # -> registry.build("data", {...})
education: "bachelors"      # -> envelope.model
personality: "warm"        # -> envelope.instructions  (composed OVER the
                            #    template's grounding, not replacing it)
skills: [...]              # -> template Spec fields / [tool.apx.tools]
```

**4. The hard, governed parts are already done** — and they're the parts you'd
least want to reimplement: OBO identity passthrough on every tool call, UC
grants enforced per-user, MLflow tracing + resource declarations, guardrails,
dual deploy targets (Model Serving *and* Apps from one definition). Coworker
gets all of it by producing engine-native agents instead of wrapping them.

## What you build vs. what you get

| You build (Coworker) | You get (engine) |
|----------------------|------------------|
| CoworkerSpec schema + the HR vocabulary | Typed `Spec` validation + JSON-schema export |
| The portal / catalog / wizard UI | `registry.list()` powering it |
| Your role templates (Assistant, Meeting Mgr…) | The `Template` protocol + entry-point registration |
| Lifecycle state machine, attribution, coaching | Build → governed, deployable agent |
| The hiring metaphor | OBO, tracing, guardrails, dual-target deploy |

The translation layer shrinks from "rebuild agent primitives in YAML" to
**"map HR words onto a registry call + a config envelope."** That's the whole
job.

## Where it's going

Sequenced so each piece unblocks the next, and so you're never blocked on us:

- **E1 · Template protocol** — *designed, ready to plan.* The foundation. Makes
  `DataAgent` a clean instance of a general, registrable pattern.
- **E2 · `[tool.apx.tools]`** — *scoped.* Declarative tools, the Level-2
  substrate. (`docs/engine-scope/02`)
- **E3 · Template-as-config** — reference a template by name + params from
  config, expanded at deploy/serve time. The bridge that lets a CoworkerSpec be
  pure data. (`docs/engine-scope/03` covers the sibling memory config.)
- **C1 / C2 · Coworker** — your repo. CoworkerSpec + translation, then catalog,
  lifecycle, portal.

Dependencies: `C → E3 → {E1, E2}`. The moment E1 + E2 + E3 land, a CoworkerSpec
is *data* that the engine compiles into a governed agent — and your repo is the
delightful experience on top, not a second agent framework.

## The pitch, in one line

**You're not building a framework on top of a framework. You're building the
front door to one that already does the hard parts — and the "Data Analyst" it
hires on day one is a primitive we already shipped.**

---

*Next: the E1 implementation plan. Worth a 30-min sync to align the CoworkerSpec
field names with the Template `Spec` / envelope seam before either side writes
code — they're designed to line up, and a quick pass keeps them that way.*
