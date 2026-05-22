# CLI reference

`apx` is the command-line wrapper. Every command maps to a single library primitive — the CLI is ergonomics, not logic.

```bash
apx scaffold my_agent                       # generate a Model Serving project (default)
apx scaffold my_agent --target apps         # generate a Databricks Apps project
cd my_agent && uv sync
apx run                            # uvicorn against app.py:app
apx publish-tools --dry-run        # preview UC function registrations
apx publish-tools                  # actually register
apx deploy --model databricks-claude-sonnet-4-6 \
           --name main.agents.my_agent              # default: --target model-serving
apx deploy --target apps                            # bundle deploy + bundle run, no container build
apx publish --endpoint my_agent --supervisor sa-12345 \
            --description "Handles X for users asking about Y"
apx mcp-config --host https://workspace.cloud.databricks.com
apx eval evalset.jsonl --model databricks-claude-sonnet-4-6
apx logs --endpoint my_agent                # runtime logs from Model Serving
apx logs --endpoint my_agent --build        # build-time logs
apx logs --app my-app --profile prod        # Databricks Apps logs (via the CLI)
apx info                                    # introspect tools, sub-agents, declared resources
apx lint                                    # static checks: instructions, docstrings, env vars, model names
apx test --prompt "what's the lineage?"     # local smoke test against a sample prompt
apx trace --agent customer_triage           # recent MLflow traces, filtered by apx.* attrs
apx list                                    # discover deployed apx-agents via UC tag scan
apx cost --agent customer_triage --hours 24 # DBU + $ over a lookback window
apx export-traces --table main.agents.traces --hours 24    # MLflow traces → Delta
apx topology --format mermaid > topology.mmd               # multi-agent graph
apx eval-chain evalset.jsonl --model X --experiment /Users/me/...  # per-prompt sub-agent coverage
apx hot-swap --endpoint customer_triage --model databricks-claude-opus-4-7  # change LLM without re-logging
apx canary deploy --endpoint customer_triage --model main.agents.x --version 42 --traffic 10
apx canary analyze --endpoint customer_triage --hours 24    # per-version requests / errors / latency
apx canary promote --endpoint customer_triage --model main.agents.x --version 42
apx canary rollback --endpoint customer_triage --model main.agents.x --version 41
apx watchdog violations --hours 24       # recent reject/redact decisions from the UC table
apx watchdog status --agent customer_triage  # current posture via watchdog's MCP tool
apx memory recall --principal-id user:alice --query "notification preferences"  # semantic recall
apx memory remember --principal-id user:alice --content "..." --importance 0.8
apx examples find --agent-id triage --query "why is my bill high?" -k 5
apx examples save --agent-id triage --input "..." --output "..." --score 0.9
```

All commands that take an agent accept `--module module:variable` to point at the agent (defaults to `agent:agent`).

A worked example exercising the full surface — `@tool(uc=...)`, `genie_tool`, `vector_search_tool`, `sql`-style tools, and `HandoffAgent` routing — lives in [`python/examples/customer_triage/`](../python/examples/customer_triage/). Read its README for the end-to-end flow from scaffold to deploy to publish.
