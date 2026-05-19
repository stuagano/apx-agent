/**
 * memory-demo — wire memory + examples into one agent end-to-end (TS edition).
 *
 * Canonical "how do I bolt memory and few-shot examples onto an
 * apx-agent" worked example. Mirrors `python/examples/memory_demo/app.py`
 * 1:1 in TS idioms.
 *
 * What this file demonstrates:
 *
 *   1. Wire an `InMemoryMemoryStore` and `InMemoryExampleStore` at module
 *      load. (Lakebase / Delta variants drop in cleanly when persistence
 *      matters.)
 *   2. Pre-seed memories + few-shot examples for a fake principal.
 *   3. Wire `recall` / `remember` / `forget` tools via `makeMemoryTools`
 *      so the LLM can call them mid-turn.
 *   4. Build the system prompt via `assembleContext`, which pulls
 *      relevant memories and few-shot examples from the stores and
 *      formats them as markdown. In a session-aware deployment this
 *      runs per turn from a callback; in this demo it runs once at
 *      module load.
 *   5. Print the round-trip end-to-end (no LLM call required — store
 *      recall and the remember tool both run in-process, so the demo is
 *      reproducible without a serving endpoint).
 *
 * Run:
 *   cd typescript/examples/memory-demo
 *   npx tsx src/app.ts
 */

import {
  InMemoryExampleStore,
  InMemoryMemoryStore,
  assembleContext,
  createAgentPlugin,
  makeMemoryTools,
  type AgentTool,
} from '../../../src/index.js';

// ---------------------------------------------------------------------------
// 1. Wire stores at module load. In production these are typically Lakebase-
// backed; the InMemory variants keep the demo runnable without infra.
// ---------------------------------------------------------------------------

const PRINCIPAL_ID = 'alice' as const;
const AGENT_ID = 'travel_concierge' as const;

const memoryStore = new InMemoryMemoryStore();
const exampleStore = new InMemoryExampleStore();

// 2. Pre-seed five memories for the demo principal. Byte-identical to the
// Python demo so recall ordering matches.
const SEED_MEMORIES: ReadonlyArray<{
  readonly content: string;
  readonly tags: readonly string[];
}> = [
  {
    content: 'alice prefers window seats on flights longer than 4 hours',
    tags: ['preference', 'seating'],
  },
  {
    content: 'alice is vegetarian and avoids dairy',
    tags: ['preference', 'diet'],
  },
  {
    content: 'alice has TSA PreCheck — KTN ending 4421',
    tags: ['profile'],
  },
  {
    content: 'alice flew BOS->SFO on 2026-01-12 (UA 526)',
    tags: ['episodic'],
  },
  {
    content: "alice's emergency contact is partner Jess at +1-555-0144",
    tags: ['profile', 'contact'],
  },
];

const SEED_EXAMPLES: ReadonlyArray<{
  readonly input: string;
  readonly output: string;
  readonly intent: string;
}> = [
  {
    input: "what's my next flight?",
    output: 'Pulling up your itinerary now.',
    intent: 'lookup',
  },
  {
    input: 'switch me to a window seat',
    output: 'Done — moved you to 14A.',
    intent: 'modify',
  },
  {
    input: 'is my meal preference saved?',
    output: 'Yes, vegetarian, no dairy is on file.',
    intent: 'lookup',
  },
];

async function seedStores(): Promise<void> {
  for (const seed of SEED_MEMORIES) {
    await memoryStore.add({
      principalId: PRINCIPAL_ID,
      namespace: 'profile',
      content: seed.content,
      tags: seed.tags,
    });
  }
  for (const ex of SEED_EXAMPLES) {
    await exampleStore.add({
      agentId: AGENT_ID,
      input: ex.input,
      output: ex.output,
      intent: ex.intent,
    });
  }
}

// ---------------------------------------------------------------------------
// 3. Build the agent's tool set via the framework's makeMemoryTools helper.
// These are `AgentTool` instances bound to the store; the LLM can invoke
// them mid-turn to recall facts or persist new ones.
// ---------------------------------------------------------------------------

const memoryTools: AgentTool[] = makeMemoryTools({
  store: memoryStore,
  defaultPrincipalId: PRINCIPAL_ID,
  namespaceDefault: 'profile',
});

// ---------------------------------------------------------------------------
// 4. Build the agent's system prompt via assembleContext — pulls relevant
// memories + few-shot examples from the stores and renders them as markdown
// blocks. In a session-aware deployment this runs per turn from a callback;
// here we resolve once for the demo prompt.
// ---------------------------------------------------------------------------

const DEMO_QUERY = 'what seat do I usually pick?';

async function buildSystemPrompt(): Promise<string> {
  const contextBlock = await assembleContext({
    memory: {
      store: memoryStore,
      opts: {
        principalId: PRINCIPAL_ID,
        query: DEMO_QUERY,
        k: 3,
      },
      header: '### What we know about the user',
    },
    examples: {
      store: exampleStore,
      opts: {
        agentId: AGENT_ID,
        query: DEMO_QUERY,
        k: 2,
      },
      header: '### Example interactions for this agent',
    },
  });

  return (
    contextBlock +
    '\n\n' +
    'You are a helpful travel concierge for the named user. Use the recall ' +
    'tool to look up additional facts mid-conversation. Use remember to ' +
    'persist new facts the user shares.'
  );
}

// ---------------------------------------------------------------------------
// 5. Reproducible demo path — no LLM endpoint required. Exercises the recall
// tool and the after-response 'remember' path directly so the user can see
// the full round-trip in stdout.
// ---------------------------------------------------------------------------

function findTool(name: string): AgentTool {
  const tool = memoryTools.find((t) => t.name === name);
  if (!tool) {
    throw new Error(`tool ${JSON.stringify(name)} not in memoryTools`);
  }
  return tool;
}

async function safeCount(): Promise<number> {
  const rows = await memoryStore.list({
    principalId: PRINCIPAL_ID,
    limit: 10_000,
  });
  return rows.length;
}

export async function runDemo(): Promise<void> {
  await seedStores();

  const systemPrompt = await buildSystemPrompt();

  // Shape-fidelity with the Python demo: build an agent plugin. Not actually
  // mounted on Express — the demo is a pure round-trip exercise, not a
  // server.
  createAgentPlugin({
    model: 'databricks-claude-sonnet-4-6',
    instructions: systemPrompt,
    tools: memoryTools,
  });

  const recallTool = findTool('recall');
  const rememberTool = findTool('remember');

  const bar = '='.repeat(72);
  const rule = '-'.repeat(72);

  console.log(bar);
  console.log('memory-demo — apx-agent memory + examples worked example');
  console.log(bar);
  console.log();
  console.log('PRINCIPAL  :', PRINCIPAL_ID);
  console.log('AGENT      :', AGENT_ID);
  console.log('DEMO QUERY :', DEMO_QUERY);
  console.log();
  console.log(rule);
  console.log(
    'System prompt assembled at module load (memories + few-shot examples)',
  );
  console.log(rule);
  console.log(systemPrompt);
  console.log();
  console.log(rule);
  console.log('Mid-turn `recall` tool call (what the agent would invoke)');
  console.log(rule);
  const recalled = await recallTool.handler({
    query: 'what does alice prefer to eat?',
  });
  console.log(recalled);
  console.log();
  console.log(rule);
  console.log('After-response `remember` tool call (new fact from the user)');
  console.log(rule);
  const remembered = await rememberTool.handler({
    content:
      'alice booked a hotel at SFO Marriott Marquis for 2026-06-10',
  });
  console.log(remembered);
  console.log();
  console.log('done.');

  const count = await safeCount();
  console.log(`final stored memory count for ${PRINCIPAL_ID}: ${count}`);
}

// Top-level invocation when the file is executed directly. This is a runnable
// demo; just call runDemo() and surface any errors.
runDemo().catch((err) => {
  console.error(err);
  process.exit(1);
});
