/**
 * Tests for canary.ts — multi-version traffic split + analysis.
 *
 * Mirrors python/tests/test_canary.py.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  CanaryReport,
  VersionMetrics,
  analyzeCanary,
  deployCanary,
  getCanaryConfig,
  promoteCanary,
  rollbackCanary,
  type ServingEndpointsClient,
  type EndpointInfo,
  type ServedEntityResponse,
  type RouteResponse,
  type TraceRow,
  type TraceSearch,
} from '../src/canary.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _se(name: string, entityName: string, entityVersion: string): ServedEntityResponse {
  return { name, entityName, entityVersion };
}

function _route(name: string, pct: number): RouteResponse {
  return { servedEntityName: name, trafficPercentage: pct };
}

function _endpointResponse(opts: {
  servedEntities: ServedEntityResponse[];
  routes?: RouteResponse[];
}): EndpointInfo {
  return {
    config: {
      servedEntities: opts.servedEntities,
      trafficConfig: { routes: opts.routes ?? [] },
    },
  };
}

function makeClient(info: EndpointInfo): {
  client: ServingEndpointsClient;
  getMock: ReturnType<typeof vi.fn>;
  updateMock: ReturnType<typeof vi.fn>;
} {
  const getMock = vi.fn(async () => info);
  const updateMock = vi.fn(async () => undefined);
  return {
    client: { get: getMock as unknown as ServingEndpointsClient['get'], updateConfig: updateMock as unknown as ServingEndpointsClient['updateConfig'] },
    getMock,
    updateMock,
  };
}

// ---------------------------------------------------------------------------
// getCanaryConfig
// ---------------------------------------------------------------------------

describe('getCanaryConfig', () => {
  it('reads served_entities + traffic_split', async () => {
    const { client } = makeClient(_endpointResponse({
      servedEntities: [
        _se('triage-1', 'main.agents.triage', '1'),
        _se('triage-2', 'main.agents.triage', '2'),
      ],
      routes: [_route('triage-1', 90), _route('triage-2', 10)],
    }));

    const cfg = await getCanaryConfig({ endpoint: 'triage', servingEndpointsClient: client });

    expect(cfg.endpoint).toBe('triage');
    expect(cfg.servedEntities).toEqual([
      { name: 'triage-1', entityName: 'main.agents.triage', entityVersion: '1' },
      { name: 'triage-2', entityName: 'main.agents.triage', entityVersion: '2' },
    ]);
    expect(cfg.trafficSplit).toEqual({ 'triage-1': 90, 'triage-2': 10 });
  });

  it('handles empty endpoint', async () => {
    const { client } = makeClient({ config: null, pendingConfig: null });

    const cfg = await getCanaryConfig({ endpoint: 'triage', servingEndpointsClient: client });

    expect(cfg.servedEntities).toEqual([]);
    expect(cfg.trafficSplit).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// deployCanary
// ---------------------------------------------------------------------------

describe('deployCanary', () => {
  it('rejects invalid traffic pct (0 and 100)', async () => {
    const { client } = makeClient(_endpointResponse({ servedEntities: [] }));
    await expect(
      deployCanary({
        endpoint: 'x',
        registeredModelName: 'main.x.y',
        newVersion: 2,
        canaryTrafficPct: 0,
        servingEndpointsClient: client,
      }),
    ).rejects.toThrow();
    await expect(
      deployCanary({
        endpoint: 'x',
        registeredModelName: 'main.x.y',
        newVersion: 2,
        canaryTrafficPct: 100,
        servingEndpointsClient: client,
      }),
    ).rejects.toThrow();
  });

  it('redistributes existing split proportionally (single existing entity)', async () => {
    const { client, updateMock } = makeClient(_endpointResponse({
      servedEntities: [_se('triage-1', 'main.agents.triage', '1')],
      routes: [_route('triage-1', 100)],
    }));

    await deployCanary({
      endpoint: 'triage',
      registeredModelName: 'main.agents.triage',
      newVersion: 2,
      canaryTrafficPct: 10,
      servingEndpointsClient: client,
    });

    expect(updateMock).toHaveBeenCalled();
    const [name, payload] = updateMock.mock.calls[0];
    expect(name).toBe('triage');
    const entities = payload.servedEntities!;
    const names = new Set(entities.map((e: { name: string }) => e.name));
    expect(names).toEqual(new Set(['triage-1', 'triage-2']));
    const routes = payload.trafficConfig!.routes;
    const split: Record<string, number> = {};
    for (const r of routes) split[r.servedEntityName] = r.trafficPercentage;
    expect(split).toEqual({ 'triage-1': 90, 'triage-2': 10 });
  });

  it('proportional split across two existing — 70/30 → 63/27/10', async () => {
    const { client, updateMock } = makeClient(_endpointResponse({
      servedEntities: [
        _se('triage-1', 'main.agents.triage', '1'),
        _se('triage-2', 'main.agents.triage', '2'),
      ],
      routes: [_route('triage-1', 70), _route('triage-2', 30)],
    }));

    await deployCanary({
      endpoint: 'triage',
      registeredModelName: 'main.agents.triage',
      newVersion: 3,
      canaryTrafficPct: 10,
      servingEndpointsClient: client,
    });

    const payload = updateMock.mock.calls[0][1];
    const routes = payload.trafficConfig!.routes;
    const split: Record<string, number> = {};
    for (const r of routes) split[r.servedEntityName] = r.trafficPercentage;

    expect(split['triage-3']).toBe(10);
    const total = Object.values(split).reduce((a, b) => a + b, 0);
    expect(total).toBe(100);
    expect(Math.abs(split['triage-1'] - 63)).toBeLessThanOrEqual(1);
    expect(Math.abs(split['triage-2'] - 27)).toBeLessThanOrEqual(1);
  });

  it('brand-new endpoint creates only the canary route', async () => {
    const { client, updateMock } = makeClient(_endpointResponse({
      servedEntities: [],
      routes: [],
    }));

    await deployCanary({
      endpoint: 'newone',
      registeredModelName: 'main.agents.newone',
      newVersion: 1,
      canaryTrafficPct: 50,
      servingEndpointsClient: client,
    });

    const payload = updateMock.mock.calls[0][1];
    const routes = payload.trafficConfig!.routes;
    const names = new Set(routes.map((r: { servedEntityName: string }) => r.servedEntityName));
    expect(names).toEqual(new Set(['newone-1']));
  });

  it('dedupes when target version already exists', async () => {
    const { client, updateMock } = makeClient(_endpointResponse({
      servedEntities: [
        _se('triage-1', 'main.agents.triage', '1'),
        _se('triage-2', 'main.agents.triage', '2'),
      ],
      routes: [_route('triage-1', 100)],
    }));

    await deployCanary({
      endpoint: 'triage',
      registeredModelName: 'main.agents.triage',
      newVersion: 2,
      canaryTrafficPct: 20,
      servingEndpointsClient: client,
    });

    const payload = updateMock.mock.calls[0][1];
    const entities = payload.servedEntities!;
    const names = entities.map((e: { name: string }) => e.name);
    expect(names.filter((n: string) => n === 'triage-2')).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// promoteCanary / rollbackCanary
// ---------------------------------------------------------------------------

describe('promoteCanary / rollbackCanary', () => {
  it('promoteCanary sets 100% to target', async () => {
    const { client, updateMock } = makeClient(_endpointResponse({
      servedEntities: [
        _se('triage-1', 'main.agents.triage', '1'),
        _se('triage-2', 'main.agents.triage', '2'),
      ],
      routes: [_route('triage-1', 90), _route('triage-2', 10)],
    }));

    await promoteCanary({
      endpoint: 'triage',
      registeredModelName: 'main.agents.triage',
      version: 2,
      servingEndpointsClient: client,
    });

    const payload = updateMock.mock.calls[0][1];
    const routes = payload.trafficConfig!.routes;
    const split: Record<string, number> = {};
    for (const r of routes) split[r.servedEntityName] = r.trafficPercentage;
    expect(split).toEqual({ 'triage-1': 0, 'triage-2': 100 });
  });

  it('promoteCanary refuses unknown target', async () => {
    const { client } = makeClient(_endpointResponse({
      servedEntities: [_se('triage-1', 'main.agents.triage', '1')],
      routes: [_route('triage-1', 100)],
    }));

    await expect(
      promoteCanary({
        endpoint: 'triage',
        registeredModelName: 'main.agents.triage',
        version: 99,
        servingEndpointsClient: client,
      }),
    ).rejects.toThrow(/isn't a served entity/);
  });

  it('rollbackCanary is functionally identical to promoteCanary', async () => {
    const { client, updateMock } = makeClient(_endpointResponse({
      servedEntities: [
        _se('triage-1', 'main.agents.triage', '1'),
        _se('triage-2', 'main.agents.triage', '2'),
      ],
      routes: [_route('triage-1', 90), _route('triage-2', 10)],
    }));

    await rollbackCanary({
      endpoint: 'triage',
      registeredModelName: 'main.agents.triage',
      version: 1,
      servingEndpointsClient: client,
    });

    const payload = updateMock.mock.calls[0][1];
    const routes = payload.trafficConfig!.routes;
    const split: Record<string, number> = {};
    for (const r of routes) split[r.servedEntityName] = r.trafficPercentage;
    expect(split).toEqual({ 'triage-1': 100, 'triage-2': 0 });
  });
});

// ---------------------------------------------------------------------------
// analyzeCanary
// ---------------------------------------------------------------------------

function _trace(opts: {
  version: string;
  status?: string;
  latencyMs?: number;
  endpoint?: string;
}): TraceRow {
  return {
    tags: {
      'apx.model.endpoint': opts.endpoint ?? 'triage',
      'apx.model.endpoint_version': opts.version,
    },
    status: opts.status ?? 'OK',
    execution_time_ms: opts.latencyMs ?? 100,
  };
}

function makeTraceSearch(rows: TraceRow[] | Error): TraceSearch {
  if (rows instanceof Error) {
    return async () => {
      throw rows;
    };
  }
  return async () => rows;
}

describe('analyzeCanary', () => {
  it('partitions traces by version', async () => {
    const rows = [
      _trace({ version: '1', latencyMs: 100 }),
      _trace({ version: '1', latencyMs: 150 }),
      _trace({ version: '2', latencyMs: 80 }),
      _trace({ version: '2', latencyMs: 90 }),
      _trace({ version: '2', latencyMs: 110 }),
    ];

    const report = await analyzeCanary({
      endpoint: 'triage',
      experiment: '/x',
      traceSearch: makeTraceSearch(rows),
    });

    const versions = Object.fromEntries(report.versions.map((v) => [v.version, v]));
    expect(versions['1'].requests).toBe(2);
    expect(versions['2'].requests).toBe(3);
    expect(versions['1'].errors).toBe(0);
    expect(versions['2'].errors).toBe(0);
  });

  it('counts errors (status != OK/SUCCESS)', async () => {
    const rows = [
      _trace({ version: '1', status: 'OK' }),
      _trace({ version: '1', status: 'ERROR' }),
      _trace({ version: '1', status: 'FAILED' }),
    ];

    const report = await analyzeCanary({
      endpoint: 'triage',
      experiment: '/x',
      traceSearch: makeTraceSearch(rows),
    });

    const v1 = report.versions.find((v) => v.version === '1')!;
    expect(v1.errors).toBe(2);
    expect(v1.requests).toBe(3);
    expect(v1.errorRate).toBeCloseTo(2 / 3);
  });

  it('computes P50 / P95 latency percentiles', async () => {
    // 100 traces at 100..199ms — nearest-rank percentile expectations
    const rows = Array.from({ length: 100 }, (_, i) =>
      _trace({ version: '1', latencyMs: 100 + i }),
    );

    const report = await analyzeCanary({
      endpoint: 'triage',
      experiment: '/x',
      traceSearch: makeTraceSearch(rows),
    });

    const v = report.versions[0];
    expect(v.latencyP50Ms).not.toBeNull();
    expect(v.latencyP50Ms!).toBeGreaterThanOrEqual(145);
    expect(v.latencyP50Ms!).toBeLessThanOrEqual(155);
    expect(v.latencyP95Ms).not.toBeNull();
    expect(v.latencyP95Ms!).toBeGreaterThanOrEqual(190);
    expect(v.latencyP95Ms!).toBeLessThanOrEqual(200);
  });

  it('filters by endpoint attribute', async () => {
    const rows = [
      _trace({ version: '1', endpoint: 'triage' }),
      _trace({ version: '1', endpoint: 'other-endpoint' }),
    ];

    const report = await analyzeCanary({
      endpoint: 'triage',
      experiment: '/x',
      traceSearch: makeTraceSearch(rows),
    });

    const total = report.versions.reduce((a, v) => a + v.requests, 0);
    expect(total).toBe(1);
  });

  it('falls back to "unknown" when version attribute missing', async () => {
    const rows: TraceRow[] = [
      { tags: {}, status: 'OK', execution_time_ms: 50 },
    ];

    const report = await analyzeCanary({
      endpoint: 'triage',
      experiment: '/x',
      traceSearch: makeTraceSearch(rows),
    });

    expect(report.versions.some((v) => v.version === 'unknown')).toBe(true);
  });

  it('returns empty report when traceSearch fails', async () => {
    const report = await analyzeCanary({
      endpoint: 'triage',
      experiment: '/x',
      traceSearch: makeTraceSearch(new Error('backend down')),
    });

    expect(report.versions).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// CanaryReport best-of helpers
// ---------------------------------------------------------------------------

describe('CanaryReport best-of helpers', () => {
  it('bestByLatency picks lowest P95', () => {
    const report = new CanaryReport({
      endpoint: 'triage',
      lookbackHours: 24,
      versions: [
        new VersionMetrics({
          version: '1',
          requests: 10,
          errors: 0,
          latencyP50Ms: 80,
          latencyP95Ms: 200,
          latencyAvgMs: 100,
        }),
        new VersionMetrics({
          version: '2',
          requests: 10,
          errors: 0,
          latencyP50Ms: 90,
          latencyP95Ms: 150,
          latencyAvgMs: 110,
        }),
      ],
    });
    const best = report.bestByLatency();
    expect(best).not.toBeNull();
    expect(best!.version).toBe('2');
  });

  it('bestByErrorRate picks lowest rate', () => {
    const report = new CanaryReport({
      endpoint: 'triage',
      lookbackHours: 24,
      versions: [
        new VersionMetrics({
          version: '1',
          requests: 100,
          errors: 5,
          latencyP50Ms: 100,
          latencyP95Ms: 200,
          latencyAvgMs: 120,
        }),
        new VersionMetrics({
          version: '2',
          requests: 100,
          errors: 2,
          latencyP50Ms: 100,
          latencyP95Ms: 200,
          latencyAvgMs: 120,
        }),
      ],
    });
    const best = report.bestByErrorRate();
    expect(best).not.toBeNull();
    expect(best!.version).toBe('2');
  });

  it('handles no versions', () => {
    const report = new CanaryReport({ endpoint: 'x', lookbackHours: 1, versions: [] });
    expect(report.bestByLatency()).toBeNull();
    expect(report.bestByErrorRate()).toBeNull();
  });
});
