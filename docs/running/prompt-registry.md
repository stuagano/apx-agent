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

## A binding does not replace agent instructions

The binding declares a prompt the agent loads. It does **not** make APX load
that prompt or replace `Agent(instructions=...)`. Your agent code must load
and format the registered prompt using MLflow (for example,
`mlflow.genai.load_prompt("prompts:/main.prompts.writer@production")`) and pass
the resulting text to `instructions=`. Register the prompt and set the alias
first, or use an explicit version URI. Install the MLflow dependency (APX's
`eval` extra includes it) and ensure the loading identity can access the prompt.

The binding stores a prompt name, not a version or alias. Version selection and
when the prompt is reloaded remain the responsibility of the code loading it;
changing an alias does not rewrite already-loaded instructions.

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
