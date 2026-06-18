# CLI reference

`apx-agent` is the command-line wrapper. Every command maps to a single library primitive — the CLI is ergonomics, not logic.

```bash
apx-agent agents scaffold my_agent          # write my_agent.yaml (default)
apx-agent agents deploy my_agent.yaml --target apps  # generate project at deploy time
apx-agent agents scaffold my_agent --no-yaml # generate an editable Apps project directory
cd my_agent && uv sync
apx-agent agents run               # uvicorn against an existing project directory
apx-agent uc publish --dry-run     # preview UC function registrations
apx-agent uc publish               # actually register
apx-agent agents deploy --model databricks-claude-sonnet-4-6 \
           --name main.agents.my_agent              # default: --target model-serving
apx-agent agents deploy --target apps               # bundle deploy + bundle run, no container build
apx-agent agents advertise --description "Handles X for users asking about Y"
apx-agent supervisor add --endpoint my_agent --supervisor sa-12345
apx-agent uc mcp-config --host https://workspace.cloud.databricks.com
apx-agent eval run evalset.jsonl --model databricks-claude-sonnet-4-6
apx-agent agents logs --endpoint my_agent           # runtime logs from Model Serving
apx-agent agents logs --endpoint my_agent --build   # build-time logs
apx-agent agents logs --app my-app --profile prod   # Databricks Apps logs (via the CLI)
apx-agent agents describe                   # introspect tools, sub-agents, declared resources
apx-agent eval lint                         # static checks: instructions, docstrings, env vars, model names
apx-agent eval test --prompt "what's the lineage?"  # local smoke test against a sample prompt
apx-agent traces list --agent customer_triage       # recent MLflow traces, filtered by apx.* attrs
apx-agent agents list                       # discover deployed apx-agents via UC tag scan
apx-agent agents cost --agent customer_triage --hours 24 # DBU + $ over a lookback window
apx-agent traces export --table main.agents.traces --hours 24    # MLflow traces → Delta
apx-agent uc topology --format mermaid > topology.mmd            # multi-agent graph
apx-agent eval chain evalset.jsonl --model X --experiment /Users/me/...  # per-prompt sub-agent coverage
apx-agent agents hot-swap --endpoint customer_triage --model databricks-claude-opus-4-7  # change LLM without re-logging
apx-agent canary deploy --endpoint customer_triage --model main.agents.x --version 42 --traffic 10
apx-agent canary analyze --endpoint customer_triage --hours 24    # per-version requests / errors / latency
apx-agent canary promote --endpoint customer_triage --model main.agents.x --version 42
apx-agent canary rollback --endpoint customer_triage --model main.agents.x --version 41
# judge alignment loop — collect SME labels, then align the BYO LLM judge
# step 1: register a judge with make_judge().register() against the agent's experiment
# step 2: open the labeling session (experiment auto-resolved from the apx.mlflow.experiment_id UC tag set at deploy)
apx-agent label start --uc-name cat.sch.my_agent --judge domain_quality --scale 1-5 --assignee sme@co.com
                                        # prints Review App URL + run-id; SMEs label out-of-band
# step 3: align the judge once labeling is complete (requires: pip install 'apx-agent[align]')
apx-agent label align --uc-name cat.sch.my_agent --judge domain_quality --run <run-id>
# for cross-environment or multi-deploy setups where runtime traces land in a different experiment,
# override the auto-resolved experiment with --experiment <mlflow-experiment-id>
apx-agent label start --uc-name cat.sch.my_agent --judge domain_quality --scale 1-5 --experiment 123456789
apx-agent watchdog violations --hours 24       # recent reject/redact decisions from the UC table
apx-agent watchdog status --agent customer_triage  # current posture via watchdog's MCP tool
apx-agent memory recall --principal-id user:alice --query "notification preferences"  # semantic recall
apx-agent memory remember --principal-id user:alice --content "..." --importance 0.8
apx-agent examples find --agent-id triage --query "why is my bill high?" -k 5
apx-agent examples save --agent-id triage --input "..." --output "..." --score 0.9
```

Commands that load the agent accept `--module module:variable` to point at the agent (defaults to `agent:agent`): the `agents run`, `agents deploy`, `agents describe`, `agents publish`, and `agents advertise` commands; the `uc publish` / `uc mcp-config` commands; and the `eval run` / `eval lint` / `eval test` / `eval chain` commands. (Commands like `agents scaffold`, `agents cost`, and the `agents pull-comments` / `agents migrate-to-okf` / `agents refresh-schema` OKF commands don't take `--module`.)

A worked example exercising the full surface — `@tool(uc=...)`, `genie_tool`, `vector_search_tool`, `sql`-style tools, and `HandoffAgent` routing — lives in [`python/examples/customer_triage/`](../python/examples/customer_triage/). Read its README for the end-to-end flow from scaffold to deploy to publish.
