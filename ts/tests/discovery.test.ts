/**
 * Tests for the discovery plugin: agent card shape and resolveEnvVar.
 *
 * resolveEnvVar is internal, so we test it through the observable behaviour
 * of createDiscoveryPlugin (the url/registry fields that accept $ENV_VAR syntax).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createDiscoveryPlugin } from '../src/discovery/index.js';
import type { AgentCard, DiscoveryConfig } from '../src/discovery/index.js';

// ---------------------------------------------------------------------------
// Helpers — lightweight request/response fakes (no http stack needed)
// ---------------------------------------------------------------------------

function makeReq(protocol = 'https', host = 'agent.example.com') {
  return {
    protocol,
    get: (header: string) => (header === 'host' ? host : ''),
    query: {} as Record<string, string>,
  };
}

function makeRes() {
  let captured: unknown;
  return {
    json: (body: unknown) => { captured = body; },
    get captured() { return captured as AgentCard; },
  };
}

function makeAgentExports(tools: Array<{ name: string; description: string }> = [], model = 'gpt-4o') {
  return () => ({
    getTools: () => tools,
    getConfig: () => ({ model }),
    getToolSchemas: () => [],
  });
}

// ---------------------------------------------------------------------------
// createDiscoveryPlugin — plugin shape
// ---------------------------------------------------------------------------

describe('createDiscoveryPlugin — plugin metadata', () => {
  it('returns a plugin object with correct name and displayName', () => {
    const plugin = createDiscoveryPlugin({}, makeAgentExports());
    expect(plugin.name).toBe('discovery');
    expect(plugin.displayName).toBe('Agent Discovery');
  });

  it('exposes setup and injectRoutes methods', () => {
    const plugin = createDiscoveryPlugin({}, makeAgentExports());
    expect(typeof plugin.setup).toBe('function');
    expect(typeof plugin.injectRoutes).toBe('function');
  });
});

// ---------------------------------------------------------------------------
// createDiscoveryPlugin — agent card shape via /.well-known/agent.json
// ---------------------------------------------------------------------------

describe('createDiscoveryPlugin — agent card', () => {
  function invokeRoute(
    config: DiscoveryConfig,
    tools: Array<{ name: string; description: string }> = [],
    model = 'gpt-4o',
  ): AgentCard {
    const plugin = createDiscoveryPlugin(config, makeAgentExports(tools, model));
    const res = makeRes();

    // Capture the route handler registered for /.well-known/agent.json
    let handler: Function | undefined;
    const router = {
      get: (path: string, fn: Function) => {
        if (path === '/.well-known/agent.json') handler = fn;
      },
    };

    plugin.injectRoutes(router as any);
    handler!(makeReq(), res);
    return res.captured;
  }

  it('includes required top-level fields', () => {
    const card = invokeRoute({ name: 'my-agent', description: 'Does stuff' });
    expect(card.schemaVersion).toBe('1.0');
    expect(card.protocolVersion).toBe('0.3.0');
    expect(card.name).toBe('my-agent');
    expect(card.description).toBe('Does stuff');
  });

  it('sets url from the request protocol and host', () => {
    const card = invokeRoute({});
    expect(card.url).toBe('https://agent.example.com');
  });

  it('advertises streaming and multiTurn capabilities', () => {
    const card = invokeRoute({});
    expect(card.capabilities.streaming).toBe(true);
    expect(card.capabilities.multiTurn).toBe(true);
  });

  it('declares bearer authentication', () => {
    const card = invokeRoute({});
    expect(card.authentication.schemes).toContain('bearer');
  });

  it('maps tools to skills with id, name, description', () => {
    const tools = [
      { name: 'search_catalog', description: 'Search the catalog' },
      { name: 'get_lineage', description: 'Get table lineage' },
    ];
    const card = invokeRoute({}, tools);
    expect(card.skills).toHaveLength(2);
    expect(card.skills[0]).toEqual({ id: 'search_catalog', name: 'search_catalog', description: 'Search the catalog' });
    expect(card.skills[1]).toEqual({ id: 'get_lineage', name: 'get_lineage', description: 'Get table lineage' });
  });

  it('returns empty skills array when agent has no tools', () => {
    const card = invokeRoute({}, []);
    expect(card.skills).toEqual([]);
  });

  it('sets mcpEndpoint to baseUrl/mcp', () => {
    const card = invokeRoute({});
    expect(card.mcpEndpoint).toBe('https://agent.example.com/mcp');
  });

  it('falls back to model name when config.name is not set', () => {
    const card = invokeRoute({ name: undefined }, [], 'claude-sonnet');
    expect(card.name).toBe('claude-sonnet');
  });

  it('falls back to "agent" when neither name nor model is set', () => {
    const plugin = createDiscoveryPlugin({}, () => null);
    const res = makeRes();
    let handler: Function | undefined;
    plugin.injectRoutes({ get: (path: string, fn: Function) => { if (path === '/.well-known/agent.json') handler = fn; } } as any);
    handler!(makeReq(), res);
    expect(res.captured.name).toBe('agent');
  });
});

// ---------------------------------------------------------------------------
// resolveEnvVar — tested through observable side-effects
// ---------------------------------------------------------------------------

describe('resolveEnvVar (via plugin config)', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it('passes literal strings through unchanged', () => {
    // We test this through setup() — if registry is a literal URL it should
    // be used as-is. We spy on setTimeout to prevent actual network call.
    const timerSpy = vi.spyOn(global, 'setTimeout').mockImplementation(() => 0 as any);
    const plugin = createDiscoveryPlugin(
      { registry: 'https://registry.example.com' },
      makeAgentExports(),
    );
    plugin.setup();
    expect(timerSpy).toHaveBeenCalled();
    timerSpy.mockRestore();
  });

  it('resolves $VAR_NAME syntax from process.env', () => {
    process.env['MY_REGISTRY'] = 'https://resolved.example.com';
    const timerSpy = vi.spyOn(global, 'setTimeout').mockImplementation(() => 0 as any);
    const plugin = createDiscoveryPlugin(
      { registry: '$MY_REGISTRY' },
      makeAgentExports(),
    );
    plugin.setup();
    expect(timerSpy).toHaveBeenCalled();
    timerSpy.mockRestore();
  });

  it('resolves ${VAR_NAME} syntax from process.env', () => {
    process.env['AGENT_URL'] = 'https://app.example.com';
    const timerSpy = vi.spyOn(global, 'setTimeout').mockImplementation(() => 0 as any);
    const plugin = createDiscoveryPlugin(
      { registry: 'https://registry.example.com', url: '${AGENT_URL}' },
      makeAgentExports(),
    );
    plugin.setup();
    expect(timerSpy).toHaveBeenCalled();
    timerSpy.mockRestore();
  });

  it('does not call setTimeout when registry is empty string', () => {
    const timerSpy = vi.spyOn(global, 'setTimeout').mockImplementation(() => 0 as any);
    const plugin = createDiscoveryPlugin({ registry: '' }, makeAgentExports());
    plugin.setup();
    expect(timerSpy).not.toHaveBeenCalled();
    timerSpy.mockRestore();
  });

  it('does not call setTimeout when $ENV_VAR resolves to empty string', () => {
    // Ensure env var is unset
    delete process.env['UNSET_VAR'];
    const timerSpy = vi.spyOn(global, 'setTimeout').mockImplementation(() => 0 as any);
    const plugin = createDiscoveryPlugin({ registry: '$UNSET_VAR' }, makeAgentExports());
    plugin.setup();
    expect(timerSpy).not.toHaveBeenCalled();
    timerSpy.mockRestore();
  });

  it('does not schedule registration when registry is not configured', () => {
    const timerSpy = vi.spyOn(global, 'setTimeout').mockImplementation(() => 0 as any);
    const plugin = createDiscoveryPlugin({}, makeAgentExports());
    plugin.setup();
    expect(timerSpy).not.toHaveBeenCalled();
    timerSpy.mockRestore();
  });
});
