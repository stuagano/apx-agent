/**
 * Tests for cost.ts — DBU + USD cost helpers over system tables.
 *
 * Mirrors python/tests/test_cost.py.
 */

import { describe, it, expect, vi } from 'vitest';
import { CostBreakdown, costForAgent, costForEndpoint, buildCostSql } from '../src/cost.js';
import type { SqlExecutor } from '../src/cost.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeExecutor(rows: Array<Record<string, unknown>>): {
  exec: SqlExecutor;
  spy: ReturnType<typeof vi.fn>;
} {
  const spy = vi.fn().mockResolvedValue(rows);
  return { exec: spy as unknown as SqlExecutor, spy };
}

function makeFailingExecutor(message = 'system.billing not accessible'): SqlExecutor {
  return vi.fn().mockRejectedValue(new Error(message)) as unknown as SqlExecutor;
}

// ---------------------------------------------------------------------------
// Argument validation
// ---------------------------------------------------------------------------

describe('costForEndpoint - argument validation', () => {
  it('rejects empty endpoint', async () => {
    const { exec } = makeExecutor([]);
    await expect(costForEndpoint({ endpoint: '', sqlExecutor: exec })).rejects.toThrow();
  });

  it('rejects zero lookbackHours', async () => {
    const { exec } = makeExecutor([]);
    await expect(
      costForEndpoint({ endpoint: 'triage', sqlExecutor: exec, lookbackHours: 0 }),
    ).rejects.toThrow();
  });

  it('rejects negative lookbackHours', async () => {
    const { exec } = makeExecutor([]);
    await expect(
      costForEndpoint({ endpoint: 'triage', sqlExecutor: exec, lookbackHours: -5 }),
    ).rejects.toThrow();
  });
});

describe('costForAgent - argument validation', () => {
  it('requires one of agentName or endpoint', async () => {
    const { exec } = makeExecutor([]);
    await expect(costForAgent({ sqlExecutor: exec })).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// SQL query construction
// ---------------------------------------------------------------------------

describe('SQL construction', () => {
  it('query includes endpoint name and lookback hours', async () => {
    const { exec, spy } = makeExecutor([]);
    await costForEndpoint({ endpoint: 'customer_triage', sqlExecutor: exec, lookbackHours: 12 });

    const sql = spy.mock.calls[0][0] as string;
    expect(sql).toContain('system.billing.usage');
    expect(sql).toContain('system.billing.list_prices');
    expect(sql).toContain('customer_triage');
    expect(sql).toContain('INTERVAL 12 HOUR');
  });

  it('default lookback is 24 hours', async () => {
    const { exec, spy } = makeExecutor([]);
    await costForEndpoint({ endpoint: 'triage', sqlExecutor: exec });

    const sql = spy.mock.calls[0][0] as string;
    expect(sql).toContain('INTERVAL 24 HOUR');
  });

  it("escapes single quotes in endpoint name", async () => {
    const { exec, spy } = makeExecutor([]);
    await costForEndpoint({ endpoint: "user's-endpoint", sqlExecutor: exec });

    const sql = spy.mock.calls[0][0] as string;
    expect(sql).toContain("user''s-endpoint");
  });

  it('buildCostSql exposes the SQL builder directly', () => {
    const sql = buildCostSql('my_endpoint', 6);
    expect(sql).toContain('my_endpoint');
    expect(sql).toContain('INTERVAL 6 HOUR');
    expect(sql).toContain('system.billing.usage');
  });
});

// ---------------------------------------------------------------------------
// Row aggregation
// ---------------------------------------------------------------------------

describe('Row aggregation', () => {
  it('breakdown aggregates rows', async () => {
    const rows = [
      { sku_name: 'MODEL_SERVING', usage_unit: 'DBU', dbus: 100.0, usd: 7.5, unit_price: 0.075 },
      { sku_name: 'WAREHOUSE', usage_unit: 'DBU', dbus: 50.0, usd: 3.0, unit_price: 0.06 },
    ];
    const { exec } = makeExecutor(rows);
    const breakdown = await costForEndpoint({ endpoint: 'triage', sqlExecutor: exec });

    expect(breakdown).toBeInstanceOf(CostBreakdown);
    expect(breakdown.endpoint).toBe('triage');
    expect(breakdown.totalDbus).toBe(150.0);
    expect(breakdown.totalUsd).toBe(10.5);
    expect(breakdown.rows).toHaveLength(2);
  });

  it('zero usd is normalised to null', async () => {
    const rows = [
      { sku_name: 'X', usage_unit: 'DBU', dbus: 10.0, usd: 0, unit_price: null },
      { sku_name: 'Y', usage_unit: 'DBU', dbus: 5.0, usd: 1.5, unit_price: 0.3 },
    ];
    const { exec } = makeExecutor(rows);
    const breakdown = await costForEndpoint({ endpoint: 'triage', sqlExecutor: exec });

    expect(breakdown.rows[0].usd).toBeNull();
    expect(breakdown.rows[1].usd).toBe(1.5);
    // Mixed pricing — partial coverage understates the bill, so total is null
    expect(breakdown.totalUsd).toBeNull();
  });

  it('null usd is normalised to null', async () => {
    const rows = [
      { sku_name: 'X', usage_unit: 'DBU', dbus: 10.0, usd: null, unit_price: null },
    ];
    const { exec } = makeExecutor(rows);
    const breakdown = await costForEndpoint({ endpoint: 'triage', sqlExecutor: exec });

    expect(breakdown.rows[0].usd).toBeNull();
    expect(breakdown.totalUsd).toBeNull();
  });

  it('all rows with prices sums to totalUsd', async () => {
    const rows = [
      { sku_name: 'A', usage_unit: 'DBU', dbus: 10.0, usd: 1.0, unit_price: 0.1 },
      { sku_name: 'B', usage_unit: 'DBU', dbus: 20.0, usd: 2.0, unit_price: 0.1 },
    ];
    const { exec } = makeExecutor(rows);
    const breakdown = await costForEndpoint({ endpoint: 'triage', sqlExecutor: exec });

    expect(breakdown.totalUsd).toBe(3.0);
    expect(breakdown.totalDbus).toBe(30.0);
  });

  it('empty rows returns empty breakdown', async () => {
    const { exec } = makeExecutor([]);
    const breakdown = await costForEndpoint({ endpoint: 'triage', sqlExecutor: exec });

    expect(breakdown.totalDbus).toBe(0);
    expect(breakdown.totalUsd).toBeNull();
    expect(breakdown.rows).toEqual([]);
    expect(breakdown.endpoint).toBe('triage');
  });
});

// ---------------------------------------------------------------------------
// CostBreakdown.fromRows directly
// ---------------------------------------------------------------------------

describe('CostBreakdown.fromRows', () => {
  it('partial pricing yields totalUsd=null', () => {
    const breakdown = CostBreakdown.fromRows({
      endpoint: 'x',
      lookbackHours: 24,
      rows: [
        { sku_name: 'A', usage_unit: 'DBU', dbus: 10, usd: 1, unit_price: 0.1 },
        { sku_name: 'B', usage_unit: 'DBU', dbus: 10, usd: null, unit_price: null },
      ],
    });
    expect(breakdown.totalDbus).toBe(20);
    expect(breakdown.totalUsd).toBeNull();
  });

  it('all priced rows sum to totalUsd', () => {
    const breakdown = CostBreakdown.fromRows({
      endpoint: 'x',
      lookbackHours: 24,
      rows: [
        { sku_name: 'A', usage_unit: 'DBU', dbus: 10, usd: 1, unit_price: 0.1 },
        { sku_name: 'B', usage_unit: 'DBU', dbus: 10, usd: 2, unit_price: 0.2 },
      ],
    });
    expect(breakdown.totalUsd).toBe(3);
  });

  it('empty rows -> totalUsd null, totalDbus 0', () => {
    const breakdown = CostBreakdown.fromRows({ endpoint: 'x', lookbackHours: 24, rows: [] });
    expect(breakdown.totalUsd).toBeNull();
    expect(breakdown.totalDbus).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Failure handling
// ---------------------------------------------------------------------------

describe('Failure handling', () => {
  it('SQL failure returns empty breakdown with warning', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const exec = makeFailingExecutor();
      const breakdown = await costForEndpoint({ endpoint: 'triage', sqlExecutor: exec });

      expect(breakdown.endpoint).toBe('triage');
      expect(breakdown.totalDbus).toBe(0);
      expect(breakdown.rows).toEqual([]);
      expect(breakdown.totalUsd).toBeNull();
      expect(warnSpy).toHaveBeenCalled();
    } finally {
      warnSpy.mockRestore();
    }
  });

  it('SQL failure does not throw', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const exec = makeFailingExecutor('any error');
      await expect(
        costForEndpoint({ endpoint: 'triage', sqlExecutor: exec }),
      ).resolves.toBeInstanceOf(CostBreakdown);
    } finally {
      warnSpy.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// costForAgent dispatching
// ---------------------------------------------------------------------------

describe('costForAgent dispatching', () => {
  it('uses agentName as endpoint by default', async () => {
    const { exec, spy } = makeExecutor([]);
    await costForAgent({ agentName: 'customer_triage', sqlExecutor: exec });

    const sql = spy.mock.calls[0][0] as string;
    expect(sql).toContain('customer_triage');
  });

  it('explicit endpoint wins over agentName', async () => {
    const { exec, spy } = makeExecutor([]);
    await costForAgent({
      agentName: 'customer_triage',
      endpoint: 'explicit-endpoint',
      sqlExecutor: exec,
    });

    const sql = spy.mock.calls[0][0] as string;
    expect(sql).toContain('explicit-endpoint');
    // agentName should NOT appear when explicit endpoint is given
    expect(sql).not.toContain('customer_triage');
  });

  it('forwards lookbackHours', async () => {
    const { exec, spy } = makeExecutor([]);
    await costForAgent({ agentName: 'triage', sqlExecutor: exec, lookbackHours: 48 });

    const sql = spy.mock.calls[0][0] as string;
    expect(sql).toContain('INTERVAL 48 HOUR');
  });
});
