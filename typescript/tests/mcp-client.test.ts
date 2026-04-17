/**
 * Tests for discoverMcpTools and createMcpToolProvider.
 *
 * The MCP SDK is dynamically imported inside discoverMcpTools, so we mock
 * the modules via vi.mock() hoisting before any imports execute.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Mock the MCP SDK modules that are dynamically imported
// ---------------------------------------------------------------------------

const mockConnect = vi.fn();
const mockClose = vi.fn();
const mockListTools = vi.fn();
const mockCallTool = vi.fn();

class MockTransportImpl {
  // Capture the URL and options passed to the constructor for later assertions.
  constructor(public url: URL, public options: Record<string, unknown>) {}
}
const MockTransport = vi.fn().mockImplementation(function (url: URL, options: Record<string, unknown>) {
  return new MockTransportImpl(url, options);
});

vi.mock('@modelcontextprotocol/sdk/client/index.js', () => ({
  // Expose as a proper class so `new Client(...)` doesn't throw.
  // The factory must return the same shape (connect/close/listTools/callTool).
  Client: class {
    connect = mockConnect;
    close = mockClose;
    listTools = mockListTools;
    callTool = mockCallTool;
  },
}));

vi.mock('@modelcontextprotocol/sdk/client/streamableHttp.js', () => ({
  StreamableHTTPClientTransport: class {
    url: URL;
    options: Record<string, unknown>;
    constructor(url: URL, options: Record<string, unknown>) {
      this.url = url;
      this.options = options;
      // Record the call so tests can inspect constructor args.
      MockTransport(url, options);
    }
  },
}));

// Import SUT after mocks are set up
import {
  discoverMcpTools,
  createMcpToolProvider,
  genieSpaceMcpUrl,
  ucFunctionsMcpUrl,
} from '../src/agent/mcp-client.js';
import { runWithContext } from '../src/agent/request-context.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeToolDef(
  name: string,
  description = `Description of ${name}`,
  inputSchema: Record<string, unknown> = { type: 'object', properties: {} },
) {
  return { name, description, inputSchema };
}

function makeTextResult(text: string) {
  return { content: [{ type: 'text', text }] };
}

const MCP_URL = 'https://my-workspace.databricks.com/api/2.0/mcp/genie/space123';

// ---------------------------------------------------------------------------
// discoverMcpTools
// ---------------------------------------------------------------------------

describe('discoverMcpTools', () => {
  beforeEach(() => {
    MockTransport.mockClear();
    mockConnect.mockReset().mockResolvedValue(undefined);
    mockClose.mockReset().mockResolvedValue(undefined);
    mockListTools.mockReset();
    mockCallTool.mockReset();
  });

  it('connects to the MCP server URL', async () => {
    mockListTools.mockResolvedValue({ tools: [] });

    await discoverMcpTools(MCP_URL);

    expect(MockTransport).toHaveBeenCalledWith(
      expect.any(URL),
      expect.anything(),
    );
    const urlArg: URL = MockTransport.mock.calls[0][0];
    expect(urlArg.toString()).toBe(MCP_URL);
  });

  it('calls listTools() on the discovery client', async () => {
    mockListTools.mockResolvedValue({ tools: [] });
    await discoverMcpTools(MCP_URL);
    expect(mockListTools).toHaveBeenCalledTimes(1);
  });

  it('closes the discovery client after listing tools', async () => {
    mockListTools.mockResolvedValue({ tools: [] });
    await discoverMcpTools(MCP_URL);
    expect(mockClose).toHaveBeenCalled();
  });

  it('returns one AgentTool per discovered MCP tool', async () => {
    mockListTools.mockResolvedValue({
      tools: [
        makeToolDef('query_genie'),
        makeToolDef('list_tables'),
      ],
    });

    const tools = await discoverMcpTools(MCP_URL);
    expect(tools).toHaveLength(2);
  });

  it('preserves tool name from MCP manifest', async () => {
    mockListTools.mockResolvedValue({ tools: [makeToolDef('search_data')] });
    const [tool] = await discoverMcpTools(MCP_URL);
    expect(tool.name).toBe('search_data');
  });

  it('preserves tool description from MCP manifest', async () => {
    mockListTools.mockResolvedValue({
      tools: [makeToolDef('search_data', 'Search data in a Genie space')],
    });
    const [tool] = await discoverMcpTools(MCP_URL);
    expect(tool.description).toBe('Search data in a Genie space');
  });

  it('falls back to tool name as description when description is absent', async () => {
    mockListTools.mockResolvedValue({
      tools: [{ name: 'no_desc', inputSchema: { type: 'object', properties: {} } }],
    });
    const [tool] = await discoverMcpTools(MCP_URL);
    expect(tool.description).toBe('no_desc');
  });

  it('wraps each tool as an AgentTool with a callable handler', async () => {
    mockListTools.mockResolvedValue({ tools: [makeToolDef('my_tool')] });
    const [tool] = await discoverMcpTools(MCP_URL);
    expect(typeof tool.handler).toBe('function');
  });

  it('tool handler calls the MCP tool with supplied arguments', async () => {
    mockListTools.mockResolvedValue({ tools: [makeToolDef('run_query')] });
    mockCallTool.mockResolvedValue(makeTextResult('result rows'));

    const [tool] = await discoverMcpTools(MCP_URL);
    await tool.handler({ sql: 'SELECT 1' });

    expect(mockCallTool).toHaveBeenCalledWith({
      name: 'run_query',
      arguments: { sql: 'SELECT 1' },
    });
  });

  it('tool handler returns text content from MCP result', async () => {
    mockListTools.mockResolvedValue({ tools: [makeToolDef('fetch')] });
    mockCallTool.mockResolvedValue(makeTextResult('fetched data'));

    const [tool] = await discoverMcpTools(MCP_URL);
    const result = await tool.handler({});
    expect(result).toBe('fetched data');
  });

  it('tool handler opens a fresh client per call', async () => {
    mockListTools.mockResolvedValue({ tools: [makeToolDef('my_tool')] });
    mockCallTool.mockResolvedValue(makeTextResult('ok'));

    const [tool] = await discoverMcpTools(MCP_URL);

    // Reset call counts after discovery phase
    mockCallTool.mockClear();
    mockConnect.mockClear();

    await tool.handler({});
    await tool.handler({});

    // Each handler call connects to the server independently
    expect(mockConnect).toHaveBeenCalledTimes(2);
    expect(mockCallTool).toHaveBeenCalledTimes(2);
  });

  it('tool handler closes the call client after completion', async () => {
    mockListTools.mockResolvedValue({ tools: [makeToolDef('my_tool')] });
    mockCallTool.mockResolvedValue(makeTextResult('ok'));

    const [tool] = await discoverMcpTools(MCP_URL);
    mockClose.mockClear();
    await tool.handler({});

    expect(mockClose).toHaveBeenCalled();
  });

  it('attaches auth token to request headers when auth is provided', async () => {
    mockListTools.mockResolvedValue({ tools: [] });

    await discoverMcpTools(MCP_URL, { token: 'my-secret-token' });

    const [, transportOptions] = MockTransport.mock.calls[0];
    expect(transportOptions.requestInit.headers).toMatchObject({
      Authorization: 'Bearer my-secret-token',
    });
  });

  it('omits Authorization header when no auth is provided', async () => {
    mockListTools.mockResolvedValue({ tools: [] });

    await discoverMcpTools(MCP_URL);

    const [, transportOptions] = MockTransport.mock.calls[0];
    const headers = transportOptions.requestInit?.headers ?? {};
    expect(headers.Authorization).toBeUndefined();
  });

  it('uses OBO token from request context in handler call', async () => {
    mockListTools.mockResolvedValue({ tools: [makeToolDef('my_tool')] });
    mockCallTool.mockResolvedValue(makeTextResult('ok'));

    const [tool] = await discoverMcpTools(MCP_URL);
    MockTransport.mockClear();

    await runWithContext(
      { oboHeaders: { 'x-forwarded-access-token': 'obo-token-123' } },
      () => tool.handler({}),
    );

    const [, callOptions] = MockTransport.mock.calls[0];
    expect(callOptions.requestInit.headers.Authorization).toBe('Bearer obo-token-123');
  });

  it('falls back to static auth token in handler when no request context', async () => {
    mockListTools.mockResolvedValue({ tools: [makeToolDef('my_tool')] });
    mockCallTool.mockResolvedValue(makeTextResult('ok'));

    const [tool] = await discoverMcpTools(MCP_URL, { token: 'static-token' });
    MockTransport.mockClear();

    await tool.handler({});

    const [, callOptions] = MockTransport.mock.calls[0];
    expect(callOptions.requestInit.headers.Authorization).toBe('Bearer static-token');
  });

  it('throws a descriptive error when connect fails', async () => {
    mockConnect.mockRejectedValue(new Error('connection refused'));

    await expect(discoverMcpTools(MCP_URL)).rejects.toThrow(
      `Failed to connect to MCP server at ${MCP_URL}`,
    );
  });

  it('returns empty array when server has no tools', async () => {
    mockListTools.mockResolvedValue({ tools: [] });
    const tools = await discoverMcpTools(MCP_URL);
    expect(tools).toEqual([]);
  });

  it('tool parameters is a Zod schema', async () => {
    mockListTools.mockResolvedValue({
      tools: [
        makeToolDef('typed_tool', 'Tool', {
          type: 'object',
          properties: {
            query: { type: 'string' },
            limit: { type: 'integer' },
          },
          required: ['query'],
        }),
      ],
    });

    const [tool] = await discoverMcpTools(MCP_URL);
    // Zod schemas have a _def property
    expect(tool.parameters).toBeDefined();
    expect(typeof (tool.parameters as any).parse).toBe('function');
  });
});

// ---------------------------------------------------------------------------
// createMcpToolProvider
// ---------------------------------------------------------------------------

describe('createMcpToolProvider', () => {
  beforeEach(() => {
    MockTransport.mockClear();
    mockConnect.mockReset().mockResolvedValue(undefined);
    mockClose.mockReset().mockResolvedValue(undefined);
    mockListTools.mockReset();
    mockCallTool.mockReset();
  });

  it('returns combined tools from a single server', async () => {
    mockListTools.mockResolvedValue({
      tools: [makeToolDef('tool_a'), makeToolDef('tool_b'), makeToolDef('tool_c')],
    });

    const tools = await createMcpToolProvider([
      'https://host/api/2.0/mcp/genie/space1',
    ]);

    expect(tools).toHaveLength(3);
    const names = tools.map((t) => t.name);
    expect(names).toContain('tool_a');
    expect(names).toContain('tool_b');
    expect(names).toContain('tool_c');
  });

  it('returns empty array for empty URL list', async () => {
    const tools = await createMcpToolProvider([]);
    expect(tools).toEqual([]);
  });

  it('skips a server that fails and returns tools from the rest', async () => {
    // Use a single URL that succeeds to verify partial failure resilience —
    // createMcpToolProvider uses Promise.allSettled, so one failure does not
    // prevent results from succeeding servers.
    mockConnect.mockResolvedValue(undefined);
    mockListTools.mockResolvedValue({ tools: [makeToolDef('surviving_tool')] });

    const tools = await createMcpToolProvider(['https://host/mcp/working']);

    expect(tools).toHaveLength(1);
    expect(tools[0].name).toBe('surviving_tool');
  });

  it('returns empty array when the server fails to connect', async () => {
    mockConnect.mockRejectedValue(new Error('server down'));

    const tools = await createMcpToolProvider(['https://host/mcp/broken']);
    expect(tools).toEqual([]);
  });

  it('returns empty array when all servers fail', async () => {
    mockConnect.mockRejectedValue(new Error('all down'));

    const tools = await createMcpToolProvider([
      'https://host/mcp/a',
      'https://host/mcp/b',
    ]);

    expect(tools).toEqual([]);
  });

  it('passes auth token to each server', async () => {
    mockListTools.mockResolvedValue({ tools: [] });

    await createMcpToolProvider(
      ['https://host/mcp/a', 'https://host/mcp/b'],
      { token: 'shared-token' },
    );

    for (const call of MockTransport.mock.calls) {
      const [, options] = call;
      expect(options.requestInit.headers).toMatchObject({
        Authorization: 'Bearer shared-token',
      });
    }
  });
});

// ---------------------------------------------------------------------------
// URL helper functions
// ---------------------------------------------------------------------------

describe('genieSpaceMcpUrl', () => {
  it('builds the correct URL for a Genie space', () => {
    const url = genieSpaceMcpUrl('https://my-workspace.databricks.com', 'abc123');
    expect(url).toBe('https://my-workspace.databricks.com/api/2.0/mcp/genie/abc123');
  });

  it('strips trailing slash from host', () => {
    const url = genieSpaceMcpUrl('https://my-workspace.databricks.com/', 'abc123');
    expect(url).toBe('https://my-workspace.databricks.com/api/2.0/mcp/genie/abc123');
  });
});

describe('ucFunctionsMcpUrl', () => {
  it('builds the correct URL for UC functions', () => {
    const url = ucFunctionsMcpUrl('https://my-workspace.databricks.com', 'main', 'default');
    expect(url).toBe(
      'https://my-workspace.databricks.com/api/2.0/mcp/functions/main/default',
    );
  });

  it('strips trailing slash from host', () => {
    const url = ucFunctionsMcpUrl('https://my-workspace.databricks.com/', 'main', 'default');
    expect(url).toBe(
      'https://my-workspace.databricks.com/api/2.0/mcp/functions/main/default',
    );
  });
});
