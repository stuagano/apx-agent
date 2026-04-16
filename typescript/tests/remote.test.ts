/**
 * Tests for RemoteAgent — card-based discovery and remote invocation.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { RemoteAgent } from '../src/workflows/remote.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeCardResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schemaVersion: '1.0',
    name: 'data-inspector',
    description: 'Inspects data',
    url: 'https://data-inspector.workspace.databricksapps.com',
    protocolVersion: '1.0',
    capabilities: { streaming: true, multiTurn: true },
    authentication: { schemes: ['bearer'], credentials: '' },
    skills: [
      { id: 'inspect', name: 'inspect', description: 'Inspect a table' },
    ],
    ...overrides,
  };
}

function makeResponsesPayload(text: string) {
  return {
    output: [
      {
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text }],
      },
    ],
  };
}

/** Build a Response-like object returned by the global fetch mock. */
function mockFetchResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    json: vi.fn().mockResolvedValue(body),
    text: vi.fn().mockResolvedValue(typeof body === 'string' ? body : JSON.stringify(body)),
    headers: {
      get: (key: string) => headers[key] ?? null,
    },
  };
}

// ---------------------------------------------------------------------------
// Global fetch mock
// ---------------------------------------------------------------------------

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.DATABRICKS_HOST;
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RemoteAgent', () => {
  const cardUrl = 'https://data-inspector.workspace.databricksapps.com/.well-known/agent.json';

  // -------------------------------------------------------------------------
  // fromCardUrl
  // -------------------------------------------------------------------------

  describe('fromCardUrl', () => {
    it('fetches the agent card on init', async () => {
      fetchMock.mockResolvedValue(mockFetchResponse(makeCardResponse()));
      await RemoteAgent.fromCardUrl(cardUrl);

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url] = fetchMock.mock.calls[0];
      expect(url).toBe(cardUrl);
    });

    it('populates card metadata after init', async () => {
      fetchMock.mockResolvedValue(mockFetchResponse(makeCardResponse()));
      const agent = await RemoteAgent.fromCardUrl(cardUrl);

      expect(agent.card).not.toBeNull();
      expect(agent.name).toBe('data-inspector');
      expect(agent.description).toBe('Inspects data');
      expect(agent.skills).toHaveLength(1);
      expect(agent.skills[0].name).toBe('inspect');
    });

    it('updates baseUrl from the card url field', async () => {
      const cardWithUrl = makeCardResponse({
        url: 'https://data-inspector.workspace.databricksapps.com',
      });
      fetchMock.mockResolvedValue(mockFetchResponse(cardWithUrl));
      const agent = await RemoteAgent.fromCardUrl(cardUrl);

      // /responses should go to the card's url, not the raw card URL
      fetchMock.mockResolvedValue(mockFetchResponse(makeResponsesPayload('hello')));
      await agent.run([{ role: 'user', content: 'hi' }]);
      const [responsesUrl] = fetchMock.mock.calls[1];
      expect(responsesUrl).toContain('/responses');
      expect(responsesUrl).not.toContain('.well-known');
    });

    it('forwards optional headers when fetching the card', async () => {
      fetchMock.mockResolvedValue(mockFetchResponse(makeCardResponse()));
      await RemoteAgent.fromCardUrl(cardUrl, { Authorization: 'Bearer token123' });

      const [, options] = fetchMock.mock.calls[0];
      expect(options.headers).toMatchObject({ Authorization: 'Bearer token123' });
    });
  });

  // -------------------------------------------------------------------------
  // fromAppName
  // -------------------------------------------------------------------------

  describe('fromAppName', () => {
    it('constructs card URL from DATABRICKS_HOST', async () => {
      process.env.DATABRICKS_HOST = 'https://my-workspace.databricks.com';
      fetchMock.mockResolvedValue(mockFetchResponse(makeCardResponse()));

      await RemoteAgent.fromAppName('my-app');

      const [url] = fetchMock.mock.calls[0];
      expect(url).toBe(
        'https://my-workspace.databricks.com/apps/my-app/.well-known/agent.json',
      );
    });

    it('strips trailing slash from DATABRICKS_HOST', async () => {
      process.env.DATABRICKS_HOST = 'https://my-workspace.databricks.com/';
      fetchMock.mockResolvedValue(mockFetchResponse(makeCardResponse()));

      await RemoteAgent.fromAppName('my-app');

      const [url] = fetchMock.mock.calls[0];
      expect(url).toBe(
        'https://my-workspace.databricks.com/apps/my-app/.well-known/agent.json',
      );
    });

    it('throws when DATABRICKS_HOST is not set', async () => {
      delete process.env.DATABRICKS_HOST;
      await expect(RemoteAgent.fromAppName('my-app')).rejects.toThrow('DATABRICKS_HOST');
    });

    it('throws when DATABRICKS_HOST is empty string', async () => {
      process.env.DATABRICKS_HOST = '';
      await expect(RemoteAgent.fromAppName('my-app')).rejects.toThrow('DATABRICKS_HOST');
    });
  });

  // -------------------------------------------------------------------------
  // Runnable interface — run()
  // -------------------------------------------------------------------------

  describe('run()', () => {
    async function makeAgent(cardOverrides: Record<string, unknown> = {}) {
      fetchMock.mockResolvedValueOnce(mockFetchResponse(makeCardResponse(cardOverrides)));
      return RemoteAgent.fromCardUrl(cardUrl);
    }

    it('returns a string', async () => {
      const agent = await makeAgent();
      fetchMock.mockResolvedValue(mockFetchResponse(makeResponsesPayload('hello world')));

      const result = await agent.run([{ role: 'user', content: 'hi' }]);
      expect(typeof result).toBe('string');
    });

    it('calls POST /responses on the remote base URL', async () => {
      const agent = await makeAgent();
      fetchMock.mockResolvedValue(mockFetchResponse(makeResponsesPayload('ok')));

      await agent.run([{ role: 'user', content: 'hi' }]);

      const [url, options] = fetchMock.mock.calls[1];
      expect(url).toMatch(/\/responses$/);
      expect(options.method).toBe('POST');
    });

    it('sends messages in the correct format', async () => {
      const agent = await makeAgent();
      fetchMock.mockResolvedValue(mockFetchResponse(makeResponsesPayload('ok')));

      const messages = [
        { role: 'user', content: 'Hello' },
        { role: 'assistant', content: 'Hi' },
        { role: 'user', content: 'Tell me more' },
      ];
      await agent.run(messages);

      const [, options] = fetchMock.mock.calls[1];
      const body = JSON.parse(options.body);
      expect(body.input).toEqual(messages);
    });

    it('sets Content-Type: application/json on requests', async () => {
      const agent = await makeAgent();
      fetchMock.mockResolvedValue(mockFetchResponse(makeResponsesPayload('ok')));

      await agent.run([{ role: 'user', content: 'hi' }]);

      const [, options] = fetchMock.mock.calls[1];
      expect(options.headers['Content-Type']).toBe('application/json');
    });

    it('extracts output_text from the response', async () => {
      const agent = await makeAgent();
      fetchMock.mockResolvedValue(mockFetchResponse(makeResponsesPayload('The answer is 42')));

      const result = await agent.run([{ role: 'user', content: 'question' }]);
      expect(result).toBe('The answer is 42');
    });

    it('falls back to JSON.stringify when output shape is unexpected', async () => {
      const agent = await makeAgent();
      fetchMock.mockResolvedValue(mockFetchResponse({ output: [] }));

      const result = await agent.run([{ role: 'user', content: 'hi' }]);
      // Should not throw — returns serialised fallback
      expect(typeof result).toBe('string');
    });

    it('forwards extra headers on every run() call', async () => {
      fetchMock.mockResolvedValue(mockFetchResponse(makeCardResponse()));
      const agent = await RemoteAgent.fromCardUrl(cardUrl, { 'X-Custom': 'custom-value' });

      fetchMock.mockResolvedValue(mockFetchResponse(makeResponsesPayload('ok')));
      await agent.run([{ role: 'user', content: 'hi' }]);

      const [, options] = fetchMock.mock.calls[1];
      expect(options.headers['X-Custom']).toBe('custom-value');
    });

    it('throws on non-2xx response from /responses', async () => {
      const agent = await makeAgent();
      fetchMock.mockResolvedValue(mockFetchResponse('Internal Server Error', 500));

      await expect(agent.run([{ role: 'user', content: 'hi' }])).rejects.toThrow('500');
    });

    it('init is idempotent — card is only fetched once across multiple run() calls', async () => {
      const agent = await makeAgent();
      fetchMock.mockResolvedValue(mockFetchResponse(makeResponsesPayload('ok')));

      await agent.run([{ role: 'user', content: '1' }]);
      await agent.run([{ role: 'user', content: '2' }]);

      // fetch called once for card, then twice for responses
      expect(fetchMock).toHaveBeenCalledTimes(3);
      const [cardFetchUrl] = fetchMock.mock.calls[0];
      expect(cardFetchUrl).toBe(cardUrl);
    });
  });

  // -------------------------------------------------------------------------
  // collectTools
  // -------------------------------------------------------------------------

  describe('collectTools()', () => {
    it('returns an empty array (remote agent has no local tools)', async () => {
      fetchMock.mockResolvedValue(mockFetchResponse(makeCardResponse()));
      const agent = await RemoteAgent.fromCardUrl(cardUrl);
      expect(agent.collectTools()).toEqual([]);
    });
  });

  // -------------------------------------------------------------------------
  // Metadata accessors before init
  // -------------------------------------------------------------------------

  describe('metadata accessors (pre-init)', () => {
    it('name returns "remote-agent" before init', () => {
      const agent = new RemoteAgent({ cardUrl });
      expect(agent.name).toBe('remote-agent');
    });

    it('description returns empty string before init', () => {
      const agent = new RemoteAgent({ cardUrl });
      expect(agent.description).toBe('');
    });

    it('skills returns empty array before init', () => {
      const agent = new RemoteAgent({ cardUrl });
      expect(agent.skills).toEqual([]);
    });

    it('card is null before init', () => {
      const agent = new RemoteAgent({ cardUrl });
      expect(agent.card).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // Error handling
  // -------------------------------------------------------------------------

  describe('error handling', () => {
    it('throws descriptive error when card fetch fails with non-ok status', async () => {
      fetchMock.mockResolvedValue(mockFetchResponse('Not Found', 404));
      await expect(RemoteAgent.fromCardUrl(cardUrl)).rejects.toThrow('404');
    });

    it('throws descriptive error when card fetch throws (network error)', async () => {
      fetchMock.mockRejectedValue(new Error('Network error'));
      await expect(RemoteAgent.fromCardUrl(cardUrl)).rejects.toThrow();
    });

    it('throws when the remote /responses endpoint returns 4xx', async () => {
      fetchMock.mockResolvedValueOnce(mockFetchResponse(makeCardResponse()));
      const agent = await RemoteAgent.fromCardUrl(cardUrl);

      fetchMock.mockResolvedValue(mockFetchResponse('Bad Request', 400));
      await expect(
        agent.run([{ role: 'user', content: 'hi' }]),
      ).rejects.toThrow();
    });
  });
});
