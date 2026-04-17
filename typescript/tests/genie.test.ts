/**
 * Tests for genieTool() factory.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { genieTool } from '../src/genie.js';

// ---------------------------------------------------------------------------
// Mock fetch
// ---------------------------------------------------------------------------

function makeMockFetch(...responses: unknown[]) {
  let call = 0;
  return vi.fn().mockImplementation(async () => {
    const body = responses[Math.min(call++, responses.length - 1)];
    return {
      ok: true,
      status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  });
}

// ---------------------------------------------------------------------------
// Factory tests
// ---------------------------------------------------------------------------

describe('genieTool factory', () => {
  it('returns a tool with default name', () => {
    const tool = genieTool('space-123', { host: 'https://host.databricks.com', oboHeaders: { Authorization: 'Bearer tok' } });
    expect(tool.name).toBe('ask_genie');
  });

  it('accepts a custom name', () => {
    const tool = genieTool('space-123', { name: 'sales_data', host: 'https://h.databricks.com', oboHeaders: { Authorization: 'Bearer tok' } });
    expect(tool.name).toBe('sales_data');
  });

  it('includes spaceId in default description', () => {
    const tool = genieTool('space-abc', { host: 'https://h.databricks.com', oboHeaders: { Authorization: 'Bearer tok' } });
    expect(tool.description).toContain('space-abc');
  });

  it('accepts a custom description', () => {
    const tool = genieTool('space-123', {
      description: 'Answer revenue questions',
      host: 'https://h.databricks.com',
      oboHeaders: { Authorization: 'Bearer tok' },
    });
    expect(tool.description).toBe('Answer revenue questions');
  });

  it('exposes a single "question" parameter', () => {
    const tool = genieTool('space-123', { host: 'https://h.databricks.com', oboHeaders: { Authorization: 'Bearer tok' } });
    const parsed = (tool.parameters as any).parse({ question: 'hello' });
    expect(parsed).toEqual({ question: 'hello' });
  });
});

// ---------------------------------------------------------------------------
// Handler tests
// ---------------------------------------------------------------------------

describe('genieTool handler', () => {
  beforeEach(() => {
    vi.stubEnv('DATABRICKS_HOST', 'https://test.databricks.com');
    vi.stubEnv('DATABRICKS_TOKEN', 'test-token');
  });

  it('returns answer on COMPLETED status', async () => {
    global.fetch = makeMockFetch(
      { conversation_id: 'conv-1', message_id: 'msg-1' },
      { status: 'COMPLETED', attachments: [{ text: { content: 'Revenue was $1M' } }] },
    );

    const tool = genieTool('space-123');
    const result = await tool.handler({ question: 'What was revenue?' });
    expect(result).toBe('Revenue was $1M');
  });

  it('calls start_conversation with the question', async () => {
    global.fetch = makeMockFetch(
      { conversation_id: 'conv-1', message_id: 'msg-1' },
      { status: 'COMPLETED', attachments: [{ text: { content: 'ok' } }] },
    );

    const tool = genieTool('space-123');
    await tool.handler({ question: 'Show me sales' });

    const firstCall = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(firstCall[0]).toContain('start_conversation');
    const body = JSON.parse(firstCall[1].body);
    expect(body.content).toBe('Show me sales');
  });

  it('returns empty string when no attachments', async () => {
    global.fetch = makeMockFetch(
      { conversation_id: 'conv-1', message_id: 'msg-1' },
      { status: 'COMPLETED', attachments: [] },
    );

    const tool = genieTool('space-123');
    const result = await tool.handler({ question: 'test' });
    expect(result).toBe('');
  });

  it('returns failure message on FAILED status', async () => {
    global.fetch = makeMockFetch(
      { conversation_id: 'conv-1', message_id: 'msg-1' },
      { status: 'FAILED', attachments: [] },
    );

    const tool = genieTool('space-123');
    const result = await tool.handler({ question: 'bad query' }) as string;
    expect(result.toLowerCase()).toContain('failed');
  });

  it('polls until COMPLETED', async () => {
    global.fetch = makeMockFetch(
      { conversation_id: 'conv-1', message_id: 'msg-1' },
      { status: 'EXECUTING_QUERY', attachments: [] },
      { status: 'COMPLETED', attachments: [{ text: { content: 'Done' } }] },
    );

    vi.useFakeTimers();
    const tool = genieTool('space-123');
    const resultPromise = tool.handler({ question: 'test' });
    await vi.runAllTimersAsync();
    const result = await resultPromise;
    expect(result).toBe('Done');
    vi.useRealTimers();
  });

  it('uses OBO header token when provided', async () => {
    global.fetch = makeMockFetch(
      { conversation_id: 'conv-1', message_id: 'msg-1' },
      { status: 'COMPLETED', attachments: [{ text: { content: 'ok' } }] },
    );

    const tool = genieTool('space-123', { oboHeaders: { Authorization: 'Bearer obo-token' } });
    await tool.handler({ question: 'test' });

    const firstCall = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(firstCall[1].headers.Authorization).toBe('Bearer obo-token');
  });
});
