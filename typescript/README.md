# appkit-agent — TypeScript port of apx-agent

The TypeScript port of [apx-agent](../README.md): declarative agents on Databricks Apps with typed tools, A2A discovery, an MCP server, and a dev UI — all as composable AppKit plugins. Same agent topology and primitives as the Python package, expressed in idiomatic TypeScript.

This package targets Node + Express apps deployed to Databricks Apps. If you want the full design narrative (governed primitives, identity passthrough, workflow patterns), read the [root README](../README.md) first — this file is a quickstart and surface map.

## Install

```bash
npm install appkit-agent @databricks/appkit zod
```

Peer dependency: `@databricks/appkit >= 0.20.0`. The package ships its own CLI binary as `apx`.

## Quick start

```typescript
import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  createDevPlugin,
  defineTool,
} from 'appkit-agent';

const getCurrentTime = defineTool({
  name: 'get_current_time',
  description: 'Get the current date and time.',
  parameters: z.object({}),
  handler: async () => new Date().toISOString(),
});

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: 'You are a helpful assistant. Use your tools when asked.',
  tools: [getCurrentTime],
});

const agentExports = () => agentPlugin.exports();

const app = express();
app.use(express.json());
agentPlugin.setup(app);
createDiscoveryPlugin({}, agentExports).setup();
createMcpPlugin({}, agentExports).setup().catch(console.error);
const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
createDiscoveryPlugin({}, agentExports).injectRoutes(app);
createMcpPlugin({}, agentExports).injectRoutes(app);
devPlugin.injectRoutes(app);

app.listen(8000, () => console.log('agent on :8000'));
```

That gives you the `/responses` agent endpoint, the `/.well-known/agent.json` A2A discovery card, the `/mcp` server, and the `/_apx/agent` dev chat UI in one process. See [`examples/basic-agent/`](./examples/basic-agent) for the full file.

## Public surface

The surface mirrors the Python package one-to-one — same names, same semantics. Where the Python doc covers a concept in depth, that's the authoritative source; what follows is the TypeScript-side map.

### Tool factories

| Factory | Purpose |
|---|---|
| `defineTool` | Zod-typed function → agent tool. The everyday tool primitive. |
| `defineUcTool` | `@tool(uc=...)` equivalent — declare a Unity Catalog function tool. |
| `ucFunctionTool` | Call an existing UC function by name. |
| `genieTool`, `genieQueryTool` | Wrap a Genie space as a natural-language tool. |
| `catalogTool`, `lineageTool`, `schemaTool` | Pre-built Unity Catalog tools. |
| `agentTool` | Wrap any agent (local or remote) as a callable tool on a parent agent. |
| `toFunctionTool`, `toolsToFunctionSchemas` | Convert defined tools into OpenAI-compatible function schemas. |

Connector tool factories for Lakebase, Vector Search, and the Doc Parser:

```typescript
import {
  createLakebaseQueryTool,
  createLakebaseMutateTool,
  createVSQueryTool,
  createVSUpsertTool,
  createDocChunkTool,
} from 'appkit-agent';
```

### Workflow agents

Deterministic composition — the structure picks the route, not the LLM.

| Agent | Purpose |
|---|---|
| `SequentialAgent` | Pipeline execution (analyze → plan → execute). |
| `ParallelAgent` | Fan-out / gather. |
| `LoopAgent` | Iterative refinement until a stop predicate fires. |
| `RouterAgent` | One LLM call picks a route, then runs that sub-agent. |
| `HandoffAgent` | Peer handoff mid-conversation (triage → specialist). |
| `RemoteAgent` | Cross-endpoint sub-agent call over HTTPS. |
| `EvolutionaryAgent` | Population-based search with Pareto frontier survival. |

When the *LLM* should decide whether to delegate, use `agentTool` instead — wrap an agent as a tool on a parent.

### Sessions

Multi-turn history persistence.

```typescript
import {
  InMemorySessionStore,
  DeltaSessionStore,
  LakebaseSessionStore,
  setDefaultSessionStore,
} from 'appkit-agent';
```

Pick by workload: `InMemorySessionStore` for tests; `LakebaseSessionStore` (Postgres) for chat-style high-frequency turns; `DeltaSessionStore` for analytics-style pipelines and cheap durability across long-idle conversations. See [`docs/lakebase-recipe.md`](../docs/running/lakebase-recipe.md) for Lakebase wiring.

### Memory (MemoryBank)

Long-lived facts per principal — survives across sessions and across handoffs.

```typescript
import {
  InMemoryMemoryStore,
  LakebaseMemoryStore,
  DeltaMemoryStore,
  makeMemoryTools,
  assembleMemoryContext,
  assembleContext,
  consolidateMemories,
} from 'appkit-agent';

const store = new LakebaseMemoryStore({
  engine,
  embeddingFn: embed,
  embeddingDim: 1024,
});

const tools = makeMemoryTools({
  store,
  defaultPrincipalId: 'user:alice',
  namespaceDefault: 'profile',
});
```

`makeMemoryTools` returns `recall` / `remember` / `forget` as defined tools. `assembleMemoryContext` renders top-k recall as a markdown block for splicing into a system prompt.

### Few-shot examples

Sibling of memory — keyed by `agent_id`, optimised for similarity retrieval over (input, output) pairs.

```typescript
import {
  InMemoryExampleStore,
  LakebaseExampleStore,
  DeltaExampleStore,
  makeExampleTools,
  assembleExampleContext,
  mineExamples,
} from 'appkit-agent';
```

`mineExamples` extracts few-shot pairs from past session history.

### Evaluation

Mosaic AI Agent Evaluation wrapper.

```typescript
import { evaluate, evaluateChain, createPredictFn, runEval } from 'appkit-agent';
```

`evaluate` is the one-shot harness; `evaluateChain` correlates eval rows with MLflow traces.

### Watchdog (compliance posture)

Full integration with the Watchdog policy service — input guards, output guards, `beforeTool`, `beforeModel`.

```typescript
import { WatchdogClient, WatchdogGuard, makeWatchdogDecision } from 'appkit-agent';
```

### Deployment + observability

| Helper | Purpose |
|---|---|
| `compileToChatAgent`, `logAgent` | MLflow ChatAgent + `log_model` (Model Serving target; injectable `mlflowLogModel`). |
| `compileToResponsesAgent`, `mountResponsesAgent` | ResponsesAgent for the Databricks Apps target (Node.js process serving `POST /invocations`). |
| `extractOboHeaders` | Single source of truth for OBO header resolution across both runtimes. |
| `mountInvocationsRoute` | Express bridge serving `/invocations` for Mosaic AI ChatAgent. |
| `hotSwapModel`, `getActiveOverride` | Swap the LLM endpoint on a deployed agent without re-logging. |
| `costForAgent`, `costForEndpoint` | DBU / $ cost reporting. |
| `deployCanary`, `promoteCanary`, `analyzeCanary` | A/B traffic split helpers. |
| `discoverTopology`, `renderTopology` | Map the agent graph across registered models. |
| `exportTraces` | MLflow traces → Delta. |
| `safeSpan`, `withSafeSpan`, `enableLangchainAutolog` | MLflow tracing helpers. |

### Two runtimes, one agent

The same `createAgentPlugin({...})` definition can be deployed to **either** Databricks Model Serving (via `compileToChatAgent` + `logAgent`) **or** Databricks Apps (via `compileToResponsesAgent` + `mountResponsesAgent`). Pick the runtime at deploy time:

| Runtime | Wire shape | Build | Deploy | OBO source |
|---|---|---|---|---|
| **Model Serving** | `ChatAgentRequest` (`messages`-style) | `mlflow.pyfunc.log_model` → UC → container image | `databricks.agents.deploy` | `customInputs.user_token` (bridged into the model from the route's `X-Forwarded-Access-Token`) |
| **Databricks Apps** | `ResponsesAgentRequest` (`input` + `customInputs`) | `tsc` build into a Node.js bundle | `databricks bundle deploy && databricks bundle run` | `X-Forwarded-Access-Token` read directly from the inbound HTTP request |

Both routes ultimately resolve OBO context through `extractOboHeaders`, so a tool sees the caller's identity regardless of which runtime served the request. Scaffold an Apps-target project with:

```bash
apx scaffold my_app --target apps
cd my_app && npm install && npm run dev      # serves /invocations on :8000
apx deploy --target apps                      # validates, deploys, polls until RUNNING
```

## CLI

The package installs `apx` as a binary. Five commands ship in the TypeScript starter (the Python CLI exposes ~21; the surface area grows wave-by-wave):

```bash
apx version                                          # installed package version
apx info --module ./agent.ts                         # introspect tools, sub-agents, declared resources
apx lint --module ./agent.ts                         # static checks: instructions, tool docs, model names
apx test --module ./agent.ts --prompt "..."          # local smoke test against a sample prompt
apx list                                             # discover apx-tagged registered models
apx scaffold <name> [--target model-serving|apps]    # generate a new agent project layout
apx deploy --target apps [--profile P] [--no-run]    # databricks bundle validate → deploy → run → poll

apx memory recall --principal-id user:alice --query "preferred channel"
apx memory remember --principal-id user:alice --content "prefers email"
apx memory forget <memory-id>
apx memory list --principal-id user:alice

apx examples find --agent-id my_agent --query "..."
apx examples save --agent-id my_agent --input "..." --output "..."
apx examples remove <example-id>
apx examples list --agent-id my_agent
```

`memory` / `examples` resolve the store from a `--store-module MODULE:VAR` flag or from `[tool.apx.agent].memory_store` / `example_store` in pyproject.toml when running side-by-side with a Python project.

## Worked examples

| Example | What it shows |
|---|---|
| [`examples/basic-agent/`](./examples/basic-agent) | The smallest end-to-end app — one tool, the four core plugins, Express. |
| [`examples/pipeline-agent/`](./examples/pipeline-agent) | `SequentialAgent` with state interpolation across steps. |
| [`examples/memory-demo/`](./examples/memory-demo) | Memory + few-shot examples — `makeMemoryTools` + `assembleContext` end-to-end. |

Each example has its own `package.json`, `app.ts`, and `databricks.yml` so you can `cd examples/<name> && npx tsx app.ts` to run locally and `./deploy.sh` to ship to Databricks Apps.

## Build / dev / test

```bash
npm install                # one-time
npm run build              # tsdown bundle → dist/
npm run dev                # tsx watch on src/index.ts
npm run typecheck          # tsc --noEmit
npm run test               # vitest run
npm run lint               # eslint src/
```

Bundle output lives in `dist/`; the binary entry is `dist/cli/index.mjs`, exposed as `apx` through the `bin` field in `package.json`.

## Equivalencies with the Python package

This package is a faithful port — same field names (camelCase here, snake_case in Python), same semantics, same store schemas. The Python README's deeper sections apply:

- [Governed primitives + identity passthrough](../README.md#1-governed-primitives)
- [Workflow patterns](../README.md#3-workflow-patterns)
- [MemoryBank](../README.md#memory-bank--long-lived-recall-across-conversations)
- [Lakebase recipe (provisioning + pgvector + token rotation)](../docs/running/lakebase-recipe.md)

When you need a primitive that exists in Python but not yet in the TS map above, file an issue — the parity goal is one-to-one.
