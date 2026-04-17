# Databricks Agent Template (TypeScript)

TS agent for Databricks Apps using FMAPI direct. No `@openai/agents` SDK.

## Quick start

1. Copy this directory
2. Replace `{{placeholders}}` in all files:

| Placeholder | Example |
|------------|---------|
| `{{AGENT_NAME}}` | `entity-resolution-agent` |
| `{{AGENT_DISPLAY_NAME}}` | `Entity Resolution Agent` |
| `{{AGENT_DESCRIPTION}}` | `Resolve and deduplicate customer entities` |
| `{{AGENT_INSTRUCTIONS}}` | `You are an entity resolution assistant...` |
| `{{WORKSPACE_HOST}}` | `https://fevm-serverless-stable-s0v155.cloud.databricks.com` |
| `{{DATABRICKS_TOKEN}}` | PAT for the workspace |

3. Add your tools in `src/tools.ts`
4. `npm install && npm run dev` to test locally
5. `./deploy.sh` to deploy

## Architecture

```
app.ts              → Express server + /responses endpoint
src/fmapi.ts        → FMAPI agent loop (model serving via fetch)
src/tools.ts        → Tool definitions (Zod schemas + handlers)
src/databricks.ts   → REST client (SQL, Jobs, Genie)
esbuild.config.mjs  → Bundle everything into dist/app.cjs
deploy.sh           → Build + deploy to Databricks Apps
```

## Key patterns

- **FMAPI direct**: `fetch(host/serving-endpoints/chat/completions)` — no intermediary SDK
- **esbuild bundle**: All deps bundled into one file — bypasses npm proxy on Databricks Apps
- **No package.json in deploy**: `databricks.yml` excludes it so Apps doesn't run `npm install`
- **DATABRICKS_HOST**: Apps injects without `https://` — `fmapi.ts` normalizes it
- **Auth**: PAT in `app.yaml` env (Apps proxy strips Authorization header)

## Adding tools

```typescript
// src/tools.ts
export const myTool = defineTool({
  name: 'my_tool',
  description: 'What it does',
  parameters: z.object({
    input: z.string().describe('The input'),
  }),
  handler: async ({ input }) => {
    // Your logic here — call Databricks APIs, external services, etc.
    return { result: input };
  },
});

// Don't forget to add it to ALL_TOOLS
export const ALL_TOOLS = [runSqlQuery, getTableInfo, myTool];
```
