# Bind a registered prompt

Use Agent Bricks' experimental Prompt Registry commands, not an APX binding
API ([ADR 0001](../adr/0001-build-on-agent-bricks.md)). They are available in
`databricks-agentbricks` 0.9.0 and later; the interface is experimental. APX's
`agentbricks` extra installs the CLI:

```bash
uv add 'apx-agent[agentbricks]'
```

## Declare the binding

Start with a durable APX project containing `agent.toml` and an existing
MLflow Prompt Registry prompt. Replace `main.prompts.writer` with the prompt's
three-part Unity Catalog name. From the project root, run:

```bash
uv run agentbricks experimental prompts bind main.prompts.writer --key writer
```

For another directory, append `--source /path/to/project`. Omitting `--key`
uses the last part of the prompt name (`writer` here). The command only edits
local `agent.toml`; it neither registers a prompt nor contacts the registry.
It adds this upstream-owned table:

```toml
[experimental.prompts]
writer = "main.prompts.writer"
```

Review and commit `agent.toml` with your agent code. To inspect the binding and
its live `@production` version, authenticate to the workspace and run:

```bash
uv run agentbricks --profile your-profile experimental prompts list
```

Unlike `bind`, `list` reads the registry when prompts are bound. To remove only
the local binding (not the registered prompt), run:

```bash
uv run agentbricks experimental prompts unbind writer
```

## A name-only binding is not loaded

The upstream value is the prompt's three-part name, not a version. APX does
not load that binding and does not replace `Agent(instructions=...)`. `doctor`
warns once per name that it is not pinned, including when the trailing comment
is an `@alias` (`prompts:/main.prompts.writer@production`). An alias can move
without a pull request, so APX does not follow it. Load an alias yourself with
`mlflow.genai.load_prompt` only when that is the behavior you want.

## Pin an exact version

Commit the upstream MLflow version URI in a trailing comment on the binding
line. The value stays `catalog.schema.name`:

```toml
[experimental.prompts]
writer = "main.prompts.writer" # prompts:/main.prompts.writer/7
```

That split is the upstream format. `AgentProject.load` accepts only a
three-part name, and `agentbricks deploy` grants with `schema_of` on that
name. Putting `prompts:/main.prompts.writer/7` in the value still has three
dot-separated pieces, so the manifest loads, but `schema_of` then grants on
`prompts:/main.prompts` instead of `main.prompts`. APX rejects a URI, an
`@alias`, or any other non-name in the value. The comment's URI name must be
the same prompt as the value. There is no new `agent.toml` key.

At startup (`compile_agent` when `project_root` is set, and `setup_agent`) APX
loads each pinned URI with `mlflow.genai.load_prompt` and checks that the
object's version is the pinned number. One plain pin, or the pin whose key is
`instructions`, replaces that agent's instructions. A template (text with
`{name}` or `{{ name }}` placeholders) is not interpolated — supplying those
variables is still open — and stays available from `loaded_prompts` in
`apx_agent._prompt_pins`. Several plain pins and no `instructions` key stay
there too; APX does not guess which one is the system prompt.

Every agent trace is tagged `apx.prompt.key`, `apx.prompt.uri`, and
`apx.prompt.version` (comma-joined, key order, when more than one prompt is
pinned). The root span also carries MLflow's native `mlflow.linkedPrompts`
trace tag, so the MLflow UI links the trace to the prompt version. See
[Resolved artifact versions](tracing.md#resolved-artifact-versions).

## Evals record the versions they scored

`evaluate` and `eval_against_endpoint` record the pinned versions on the MLflow
eval run. Each run gets the `apx.prompt.*` tags. Each prompt version is also
linked to the run with MLflow's `MlflowClient.link_prompt_version_to_run`,
which writes the run's `mlflow.linkedPrompts` tag. On Unity Catalog it also
records the registry's prompt-to-run link. If an MLflow run is already active,
the tags and links go on that run and it stays open. Otherwise APX opens a run,
`mlflow.genai.evaluate` reuses it, and APX closes it. When nothing is pinned,
APX opens no run. A failed link fails the eval before any row is scored.

`evaluate` compiles the agent in-process, so the run records what that agent
loaded. `eval_against_endpoint` scores a deployed App. It loads the pins in
`./agent.toml` with the same checks: a missing or unauthorized pin fails before
any request is sent. The run records those pins, so run it from the checkout
the App was deployed from. The App's own traces carry the versions it loaded
at startup.

For a judge whose instructions live in the Prompt Registry, build it with
`pinned_judge`:

```python
from apx_agent import evaluate, pinned_judge

tone = pinned_judge("tone", "prompts:/main.judges.tone/3", model="databricks")
evaluate(agent, model="databricks-claude-sonnet-4-6", evalset=rows, scorers=[tone])
```

`pinned_judge` loads that exact version and checks the version number. It
passes the text to `mlflow.genai.judges.make_judge`, and passes any other
keyword arguments through. An `@alias` or a URI with no version raises. The
eval run records the judge as `apx.judge_prompt.key` (the judge name),
`apx.judge_prompt.uri`, and `apx.judge_prompt.version`, and links the version
like an agent prompt. Judge prompts go on eval runs only, never on agent
traces. A judge you build yourself from `make_judge(instructions=...)` has no
registry version, so nothing records it.

Startup fails when the pin cannot be loaded. The error names the key, the
URI, and the exception. The loading identity needs `USE SCHEMA`, `EXECUTE`,
`CREATE FUNCTION`, and `MANAGE` on the prompt schema. `agentbricks deploy`
grants those on its own path. It does not grant them for `apps_api` or App
Space. Binding alone grants nothing on those paths.

`apx-agent doctor` reports each binding. A missing or unauthorized pin fails
the check when the workspace is configured. `--offline`, or doctor without
Databricks auth, records the pin and does not contact the registry.

## Promotion is a pull request

Change the comment's version and review that diff. Nothing at runtime edits
the registry or the file. `open_prompt_pin_pr` in `apx_agent._prompt_pins`
rewrites an existing pin (it does not create a binding) and runs
`git checkout -b`, `git add agent.toml`, `git commit`, and `gh pr create`.
Registering the optimizer's winning text as a new registry version needs a
workspace; this helper only bumps a version you already have. Re-running
`agentbricks experimental prompts bind` rewrites the value and drops the
comment, so put the pin comment back after a rebind.

On the upstream `agentbricks deploy` path, Agent Bricks uses bound prompts to
grant the app's service principal registry access. The upstream command documents
`USE SCHEMA`, `EXECUTE`, `CREATE FUNCTION`, and `MANAGE` on the prompt's schema;
review those grants with your administrator. Do not assume this provisioning
occurs on APX's `apps_api` or App Space deployment paths: arrange the required
access separately there. Binding alone grants nothing.

## Regeneration and deploy preserve bindings

`agent.toml` is authored project configuration. APX scaffolding creates it only
when missing; re-materializing the project leaves an existing file unchanged.
Deploy staging copies the authored file byte for byte, including prompt
bindings and comments. The one-time `apx-agent agents migrate-agent-toml`
command refuses to overwrite an existing manifest.

`build_native_manifest` is a fresh-manifest compiler, not a merge API for an
existing file. Its generated text alone contains no hand-added bindings. Do
not replace your authored manifest with that output or delete `agent.toml` to
refresh it: a newly created manifest cannot recover deleted bindings. Use the
upstream CLI to edit bindings in place.
