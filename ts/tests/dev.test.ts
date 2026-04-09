/**
 * Tests for the dev UI plugin: production guard and route registration.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createDevPlugin } from '../src/dev/index.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeAgentExports(toolNames: string[] = []) {
  return () => ({
    getTools: () => toolNames.map((name) => ({ name, description: `${name} description` })),
    getConfig: () => ({ model: 'gpt-4o' }),
    getToolSchemas: () => toolNames.map((name) => ({ function: { name } })),
  });
}

type RouteMap = Record<string, Function>;

function makeRouter(): { router: { get: (path: string, fn: Function) => void }; routes: RouteMap } {
  const routes: RouteMap = {};
  return {
    routes,
    router: {
      get: (path: string, fn: Function) => { routes[path] = fn; },
    },
  };
}

function makeRes() {
  let body: unknown;
  let statusCode = 200;
  let contentType = '';
  return {
    json: (b: unknown) => { body = b; },
    status: (code: number) => ({ json: (b: unknown) => { statusCode = code; body = b; } }),
    type: (ct: string) => { contentType = ct; return { send: (b: unknown) => { body = b; } }; },
    get body() { return body; },
    get statusCode() { return statusCode; },
    get contentType() { return contentType; },
  };
}

// ---------------------------------------------------------------------------
// Plugin metadata
// ---------------------------------------------------------------------------

describe('createDevPlugin — metadata', () => {
  it('returns a plugin with name devUI', () => {
    const plugin = createDevPlugin({}, makeAgentExports());
    expect(plugin.name).toBe('devUI');
  });

  it('exposes an injectRoutes method', () => {
    const plugin = createDevPlugin({}, makeAgentExports());
    expect(typeof plugin.injectRoutes).toBe('function');
  });
});

// ---------------------------------------------------------------------------
// Production guard
// ---------------------------------------------------------------------------

describe('createDevPlugin — production guard', () => {
  const originalEnv = process.env.NODE_ENV;

  afterEach(() => {
    process.env.NODE_ENV = originalEnv;
  });

  it('does not register routes in production when productionGuard is true (default)', () => {
    process.env.NODE_ENV = 'production';
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(Object.keys(routes)).toHaveLength(0);
  });

  it('registers routes in production when productionGuard is false', () => {
    process.env.NODE_ENV = 'production';
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({ productionGuard: false }, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(Object.keys(routes).length).toBeGreaterThan(0);
  });

  it('registers routes in development (default productionGuard)', () => {
    process.env.NODE_ENV = 'development';
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(Object.keys(routes).length).toBeGreaterThan(0);
  });

  it('registers routes when NODE_ENV is not set', () => {
    delete process.env.NODE_ENV;
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(Object.keys(routes).length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// Route registration — default basePath /_apx
// ---------------------------------------------------------------------------

describe('createDevPlugin — default basePath routes', () => {
  beforeEach(() => {
    process.env.NODE_ENV = 'development';
  });

  afterEach(() => {
    delete process.env.NODE_ENV;
  });

  it('registers /_apx/tools route', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(routes['/_apx/tools']).toBeDefined();
  });

  it('registers /_apx/agent route', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(routes['/_apx/agent']).toBeDefined();
  });

  it('registers /_apx/probe route', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(routes['/_apx/probe']).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Route registration — custom basePath
// ---------------------------------------------------------------------------

describe('createDevPlugin — custom basePath', () => {
  beforeEach(() => {
    process.env.NODE_ENV = 'development';
  });

  afterEach(() => {
    delete process.env.NODE_ENV;
  });

  it('uses the configured basePath for all routes', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({ basePath: '/dev' }, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(routes['/dev/tools']).toBeDefined();
    expect(routes['/dev/agent']).toBeDefined();
    expect(routes['/dev/probe']).toBeDefined();
  });

  it('does not register /_apx routes when custom basePath is set', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({ basePath: '/dev' }, makeAgentExports());
    plugin.injectRoutes(router as any);
    expect(routes['/_apx/tools']).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// /_apx/tools handler
// ---------------------------------------------------------------------------

describe('createDevPlugin — /_apx/tools handler', () => {
  beforeEach(() => {
    process.env.NODE_ENV = 'development';
  });

  afterEach(() => {
    delete process.env.NODE_ENV;
  });

  it('returns tool names and descriptions from agent exports', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports(['search_catalog', 'get_lineage']));
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/tools']({}, res);

    const body = res.body as { tools: Array<{ name: string; description: string }>; schemas: unknown[] };
    expect(body.tools).toHaveLength(2);
    expect(body.tools[0].name).toBe('search_catalog');
    expect(body.tools[1].name).toBe('get_lineage');
  });

  it('includes schemas alongside tools', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports(['tool_a']));
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/tools']({}, res);

    const body = res.body as { tools: unknown[]; schemas: unknown[] };
    expect(Array.isArray(body.schemas)).toBe(true);
  });

  it('returns a message when agentExports returns null', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, () => null);
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/tools']({}, res);

    const body = res.body as { tools: unknown[]; message: string };
    expect(body.tools).toEqual([]);
    expect(body.message).toContain('not available');
  });
});

// ---------------------------------------------------------------------------
// /_apx/agent handler
// ---------------------------------------------------------------------------

describe('createDevPlugin — /_apx/agent handler', () => {
  beforeEach(() => {
    process.env.NODE_ENV = 'development';
  });

  afterEach(() => {
    delete process.env.NODE_ENV;
  });

  it('returns HTML content', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports());
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/agent']({}, res);

    expect(res.contentType).toBe('html');
    expect(typeof res.body).toBe('string');
    expect((res.body as string).includes('<!DOCTYPE html>')).toBe(true);
  });

  it('embeds the basePath in the HTML for nav links', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({ basePath: '/mydev' }, makeAgentExports());
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/mydev/agent']({}, res);

    expect((res.body as string).includes('/mydev')).toBe(true);
  });

  it('includes a title referencing Agent Dev UI', () => {
    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, makeAgentExports());
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/agent']({}, res);

    expect((res.body as string).includes('Agent Dev UI')).toBe(true);
  });
});
