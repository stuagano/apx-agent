/**
 * Test script: verify POST /responses with stream: true returns SSE events.
 *
 * Usage:
 *   # Start the basic-agent first:
 *   DATABRICKS_HOST=... DATABRICKS_TOKEN=... npx tsx app.ts
 *
 *   # Then in another terminal:
 *   npx tsx test-streaming.ts
 *
 * Or run programmatically via the test suite (see tests/streaming.test.ts).
 */

const BASE_URL = process.env.AGENT_URL ?? 'http://localhost:8000';

interface SSEEvent {
  event: string;
  data: unknown;
}

async function testStreaming(): Promise<void> {
  console.log(`Testing SSE streaming against ${BASE_URL}/responses ...`);

  const res = await fetch(`${BASE_URL}/responses`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      input: [{ role: 'user', content: 'What is 2+2?' }],
      stream: true,
    }),
  });

  if (!res.ok) {
    console.error(`Request failed: ${res.status} ${res.statusText}`);
    process.exit(1);
  }

  const contentType = res.headers.get('content-type') ?? '';
  if (!contentType.includes('text/event-stream')) {
    console.error(`Expected text/event-stream but got: ${contentType}`);
    process.exit(1);
  }

  console.log('Content-Type is text/event-stream');

  const events: SSEEvent[] = [];
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';

    let currentEvent = '';
    for (const line of lines) {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith('data: ')) {
        const dataStr = line.slice(6).trim();
        try {
          events.push({ event: currentEvent, data: JSON.parse(dataStr) });
        } catch {
          events.push({ event: currentEvent, data: dataStr });
        }
      }
    }
  }

  console.log(`Received ${events.length} SSE events:`);
  for (const evt of events) {
    console.log(`  event: ${evt.event}`);
  }

  // Verify expected event types
  const eventTypes = events.map((e) => e.event);
  const hasStart = eventTypes.includes('response.output_item.start');
  const hasDelta = eventTypes.includes('output_text.delta');
  const hasDone = eventTypes.includes('response.output_item.done');

  if (hasStart) console.log('  [pass] response.output_item.start received');
  else console.log('  [FAIL] missing response.output_item.start');

  if (hasDelta) console.log('  [pass] output_text.delta received');
  else console.log('  [FAIL] missing output_text.delta');

  if (hasDone) console.log('  [pass] response.output_item.done received');
  else console.log('  [FAIL] missing response.output_item.done');

  if (hasStart && hasDone) {
    console.log('\nStreaming test PASSED');
  } else {
    console.log('\nStreaming test FAILED');
    process.exit(1);
  }
}

testStreaming().catch((err) => {
  console.error('Test error:', err);
  process.exit(1);
});
