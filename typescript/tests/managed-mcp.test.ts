/**
 * Tests for managed-mcp.ts — URL generation and client config from
 * declared ResourceSpecs. Mirrors python/tests/test_managed_mcp.py.
 *
 * Covers:
 *   1. URL templates per kind (uc_function / genie_space / vector_search_index).
 *   2. Host normalization (scheme injection, trailing slash trim).
 *   3. Unsupported kinds (serving_endpoint, malformed UC ids) reported with
 *      url=null, unsupported=true.
 *   4. OAuth scope per kind matches the Databricks docs.
 *   5. managedMcpClientConfig produces the standard mcpServers shape;
 *      entries are keyed by `${name}.${kind}.${identifier}`.
 *   6. includeUnsupported flag toggles inclusion of unsupported endpoints.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  managedMcpUrls,
  managedMcpClientConfig,
  type ManagedMCPEndpoint,
  type ResourceSpec,
} from '../src/managed-mcp.js';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const HOST = 'https://workspace.cloud.databricks.com';

function multiResourceSpecs(): ResourceSpec[] {
  return [
    { kind: 'uc_function', identifier: 'main.tools.classify_intent' },
    { kind: 'uc_function', identifier: 'main.tools.score_customer' },
    { kind: 'genie_space', identifier: 'space-abc-123' },
    { kind: 'vector_search_index', identifier: 'main.search.docs_index' },
  ];
}

// ---------------------------------------------------------------------------
// URL generation per kind
// ---------------------------------------------------------------------------

describe('managedMcpUrls — URL templates per kind', () => {
  it('builds the uc_function URL and unity-catalog scope', () => {
    const endpoints = managedMcpUrls({
      resourceSpecs: [{ kind: 'uc_function', identifier: 'main.tools.classify_intent' }],
      workspaceHost: HOST,
    });
    const ucEp = endpoints.find((e) => e.kind === 'uc_function')!;
    expect(ucEp.url).toBe(`${HOST}/api/2.0/mcp/functions/main/tools/classify_intent`);
    expect(ucEp.oauthScope).toBe('unity-catalog');
    expect(ucEp.unsupported).toBe(false);
  });

  it('builds the genie_space URL and genie scope', () => {
    const endpoints = managedMcpUrls({
      resourceSpecs: [{ kind: 'genie_space', identifier: 'space-abc-123' }],
      workspaceHost: HOST,
    });
    const genieEp = endpoints.find((e) => e.kind === 'genie_space')!;
    expect(genieEp.url).toBe(`${HOST}/api/2.0/mcp/genie/space-abc-123`);
    expect(genieEp.oauthScope).toBe('genie');
    expect(genieEp.unsupported).toBe(false);
  });

  it('builds the vector_search_index URL and vector-search scope', () => {
    const endpoints = managedMcpUrls({
      resourceSpecs: [{ kind: 'vector_search_index', identifier: 'main.search.docs_index' }],
      workspaceHost: HOST,
    });
    const vsEp = endpoints.find((e) => e.kind === 'vector_search_index')!;
    expect(vsEp.url).toBe(`${HOST}/api/2.0/mcp/vector-search/main/search/docs_index`);
    expect(vsEp.oauthScope).toBe('vector-search');
    expect(vsEp.unsupported).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Host normalization
// ---------------------------------------------------------------------------

describe('managedMcpUrls — host normalisation', () => {
  const variants = [
    'https://workspace.cloud.databricks.com',
    'https://workspace.cloud.databricks.com/',
    'workspace.cloud.databricks.com',
    'workspace.cloud.databricks.com/',
  ];

  for (const host of variants) {
    it(`normalises ${JSON.stringify(host)} consistently`, () => {
      const endpoints = managedMcpUrls({
        resourceSpecs: [{ kind: 'uc_function', identifier: 'main.tools.f' }],
        workspaceHost: host,
      });
      const ucEp = endpoints.find((e) => e.kind === 'uc_function')!;
      expect(ucEp.url).toBe(
        'https://workspace.cloud.databricks.com/api/2.0/mcp/functions/main/tools/f',
      );
    });
  }
});

// ---------------------------------------------------------------------------
// Unsupported kinds
// ---------------------------------------------------------------------------

describe('managedMcpUrls — unsupported kinds', () => {
  it('marks serving_endpoint as unsupported when model=... is supplied', () => {
    const endpoints = managedMcpUrls({
      resourceSpecs: [{ kind: 'uc_function', identifier: 'main.tools.f' }],
      workspaceHost: HOST,
      model: 'databricks-claude-sonnet-4-6',
    });
    const serving = endpoints.filter((e) => e.kind === 'serving_endpoint');
    expect(serving).toHaveLength(1);
    expect(serving[0].unsupported).toBe(true);
    expect(serving[0].url).toBeNull();
    expect(serving[0].oauthScope).toBeNull();
  });

  it('marks UC function with malformed identifier as unsupported', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const endpoints = managedMcpUrls({
        resourceSpecs: [{ kind: 'uc_function', identifier: 'not_three_parts' }],
        workspaceHost: HOST,
      });
      const ucEps = endpoints.filter((e) => e.kind === 'uc_function');
      expect(ucEps).toHaveLength(1);
      expect(ucEps[0].url).toBeNull();
      expect(ucEps[0].oauthScope).toBeNull();
      expect(ucEps[0].unsupported).toBe(true);
      expect(warnSpy).toHaveBeenCalled();
    } finally {
      warnSpy.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// Composite walk
// ---------------------------------------------------------------------------

describe('managedMcpUrls — walks mixed-kind resource lists', () => {
  it('returns endpoints for every declared spec, preserving kind set', () => {
    const endpoints = managedMcpUrls({
      resourceSpecs: multiResourceSpecs(),
      workspaceHost: HOST,
    });
    const kinds = new Set(endpoints.map((e) => e.kind));
    expect(kinds.has('uc_function')).toBe(true);
    expect(kinds.has('genie_space')).toBe(true);
    expect(kinds.has('vector_search_index')).toBe(true);
    // Every endpoint here is supported
    expect(endpoints.every((e) => e.unsupported === false)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Client config
// ---------------------------------------------------------------------------

describe('managedMcpClientConfig', () => {
  it('produces the standard mcpServers shape', () => {
    const endpoints: ManagedMCPEndpoint[] = [
      {
        kind: 'uc_function',
        identifier: 'main.tools.classify',
        url: 'https://x.cloud.databricks.com/api/2.0/mcp/functions/main/tools/classify',
        oauthScope: 'unity-catalog',
        unsupported: false,
      },
      {
        kind: 'genie_space',
        identifier: 'space-abc',
        url: 'https://x.cloud.databricks.com/api/2.0/mcp/genie/space-abc',
        oauthScope: 'genie',
        unsupported: false,
      },
    ];
    const config = managedMcpClientConfig({ endpoints });
    expect(config).toHaveProperty('mcpServers');
    const servers = config.mcpServers;
    const ucKey = 'databricks.uc_function.main.tools.classify';
    expect(servers).toHaveProperty(ucKey);
    expect(servers[ucKey].type).toBe('http');
    expect(servers[ucKey].url).toMatch(/\/api\/2\.0\/mcp\/functions\/main\/tools\/classify$/);
    expect(servers[ucKey].oauth_scope).toBe('unity-catalog');
  });

  it('skips unsupported endpoints by default', () => {
    const endpoints: ManagedMCPEndpoint[] = [
      {
        kind: 'serving_endpoint',
        identifier: 'model-foo',
        url: null,
        oauthScope: null,
        unsupported: true,
      },
    ];
    const config = managedMcpClientConfig({ endpoints });
    expect(config.mcpServers).toEqual({});
  });

  it('includes unsupported endpoints when includeUnsupported=true', () => {
    const endpoints: ManagedMCPEndpoint[] = [
      {
        kind: 'serving_endpoint',
        identifier: 'model-foo',
        url: null,
        oauthScope: null,
        unsupported: true,
      },
    ];
    const config = managedMcpClientConfig({ endpoints, includeUnsupported: true });
    const key = 'databricks.serving_endpoint.model-foo';
    expect(config.mcpServers).toHaveProperty(key);
    expect(config.mcpServers[key].unsupported).toBe(true);
  });

  it('honours a custom name prefix in mcpServers keys', () => {
    const endpoints: ManagedMCPEndpoint[] = [
      {
        kind: 'genie_space',
        identifier: 'space-abc',
        url: 'https://x/api/2.0/mcp/genie/space-abc',
        oauthScope: 'genie',
        unsupported: false,
      },
    ];
    const config = managedMcpClientConfig({ endpoints, name: 'my-agent' });
    expect(config.mcpServers).toHaveProperty('my-agent.genie_space.space-abc');
  });
});
