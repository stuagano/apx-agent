import { type Hypothesis, compositeFitness } from './hypothesis.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PopulationStoreConfig {
  host?: string;
  populationTable: string;
  warehouseId?: string;
  chunkSize?: number;
  cacheEnabled?: boolean;
}

interface SqlStatementResponse {
  statement_id: string;
  status: { state: string };
  manifest?: {
    schema?: {
      columns?: Array<{ name: string }>;
    };
  };
  result?: {
    data_array?: Array<Array<string | null>>;
  };
}

// ---------------------------------------------------------------------------
// PopulationStore
// ---------------------------------------------------------------------------

export class PopulationStore {
  private host: string;
  private populationTable: string;
  private warehouseId: string;
  private chunkSize: number;
  private cacheEnabled: boolean;
  private cache: Map<number, Hypothesis[]>;

  constructor(config: PopulationStoreConfig) {
    const rawHost = config.host ?? process.env.DATABRICKS_HOST;
    if (!rawHost) {
      throw new Error('No Databricks host: pass host in config or set DATABRICKS_HOST env var');
    }
    const normalized = rawHost.startsWith('http') ? rawHost : `https://${rawHost}`;
    this.host = normalized.replace(/\/$/, '');

    this.populationTable = config.populationTable;

    const wh = config.warehouseId ?? process.env.DATABRICKS_WAREHOUSE_ID;
    if (!wh) {
      throw new Error('No warehouse ID: pass warehouseId in config or set DATABRICKS_WAREHOUSE_ID env var');
    }
    this.warehouseId = wh;

    this.chunkSize = config.chunkSize ?? 25;
    this.cacheEnabled = config.cacheEnabled ?? true;
    this.cache = new Map();
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  async writeHypotheses(hypotheses: Hypothesis[]): Promise<void> {
    if (hypotheses.length === 0) return;

    for (let i = 0; i < hypotheses.length; i += this.chunkSize) {
      const chunk = hypotheses.slice(i, i + this.chunkSize);
      const valuesList = chunk.map((h) => {
        const id = esc(h.id);
        const generation = h.generation;
        const parentId = esc(h.parent_id ?? '');
        const fitness = esc(JSON.stringify(h.fitness));
        const metadata = esc(JSON.stringify(h.metadata));
        const flagged = h.flagged_for_review ? 'true' : 'false';
        const createdAt = esc(h.created_at);
        return `('${id}', ${generation}, '${parentId}', '${fitness}', '${metadata}', ${flagged}, '${createdAt}')`;
      });

      const statement = `INSERT INTO ${this.populationTable} (id, generation, parent_id, fitness, metadata, flagged_for_review, created_at) VALUES ${valuesList.join(', ')}`;
      await this.executeSql(statement);
    }

    // Invalidate cache after any write
    this.cache.clear();
  }

  async updateFitnessScores(updates: Array<{ id: string; fitness: Record<string, number> }>): Promise<void> {
    for (const { id, fitness } of updates) {
      const escapedId = esc(id);
      const escapedFitness = esc(JSON.stringify(fitness));
      const statement = `MERGE INTO ${this.populationTable} AS target USING (SELECT '${escapedId}' AS id, '${escapedFitness}' AS fitness) AS source ON target.id = source.id WHEN MATCHED THEN UPDATE SET target.fitness = source.fitness`;
      await this.executeSql(statement);
    }

    // Invalidate cache after any write
    this.cache.clear();
  }

  async loadGeneration(generation: number): Promise<Hypothesis[]> {
    if (this.cacheEnabled && this.cache.has(generation)) {
      return this.cache.get(generation)!;
    }

    const statement = `SELECT * FROM ${this.populationTable} WHERE generation = ${generation}`;
    const response = await this.executeSql(statement);
    const hypotheses = this.parseRows(response);

    if (this.cacheEnabled) {
      this.cache.set(generation, hypotheses);
    }

    return hypotheses;
  }

  async loadTopSurvivors(
    generation: number,
    topN: number,
    weights: Record<string, number>,
  ): Promise<Hypothesis[]> {
    const all = await this.loadGeneration(generation);
    return all
      .slice()
      .sort((a, b) => compositeFitness(b, weights) - compositeFitness(a, weights))
      .slice(0, topN);
  }

  async getFitnessHistory(
    nGenerations: number,
    weights: Record<string, number>,
  ): Promise<Array<{ generation: number; best: number; avg: number }>> {
    const statement = `SELECT * FROM ${this.populationTable}`;
    const response = await this.executeSql(statement);
    const allHypotheses = this.parseRows(response);

    // Group by generation
    const byGen = new Map<number, Hypothesis[]>();
    for (const h of allHypotheses) {
      const existing = byGen.get(h.generation) ?? [];
      existing.push(h);
      byGen.set(h.generation, existing);
    }

    // Sort generations and compute stats
    const sortedGens = Array.from(byGen.keys()).sort((a, b) => a - b);
    const history = sortedGens.map((gen) => {
      const hypotheses = byGen.get(gen)!;
      const scores = hypotheses.map((h) => compositeFitness(h, weights));
      const best = scores.length > 0 ? Math.max(...scores) : 0;
      const avg = scores.length > 0 ? scores.reduce((s, v) => s + v, 0) / scores.length : 0;
      return { generation: gen, best, avg };
    });

    return history.slice(-nGenerations);
  }

  async getActiveConstraints(): Promise<Array<Record<string, unknown>>> {
    const reviewTable = `${this.populationTable}_review_queue`;
    const statement = `SELECT * FROM ${reviewTable} WHERE status = 'approved'`;
    const response = await this.executeSql(statement);
    return this.parseRows(response).map((h) => h as unknown as Record<string, unknown>);
  }

  clearCache(): void {
    this.cache.clear();
  }

  // -------------------------------------------------------------------------
  // Internal helpers
  // -------------------------------------------------------------------------

  private async executeSql(statement: string): Promise<SqlStatementResponse> {
    const token = process.env.DATABRICKS_TOKEN;
    if (!token) {
      throw new Error('No Databricks token: set DATABRICKS_TOKEN env var');
    }

    const url = `${this.host}/api/2.0/sql/statements/`;
    const body = {
      statement,
      warehouse_id: this.warehouseId,
      wait_timeout: '30s',
      on_wait_timeout: 'CANCEL',
      disposition: 'INLINE',
      format: 'JSON_ARRAY',
    };

    const res = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`Databricks SQL API ${res.status}: ${text}`);
    }

    return res.json() as Promise<SqlStatementResponse>;
  }

  private parseRows(response: SqlStatementResponse): Hypothesis[] {
    const columns = response.manifest?.schema?.columns ?? [];
    const dataArray = response.result?.data_array ?? [];

    return dataArray.map((row) => {
      const obj: Record<string, string | null> = {};
      columns.forEach((col, i) => {
        obj[col.name] = row[i] ?? null;
      });

      return {
        id: obj['id'] ?? '',
        generation: Number(obj['generation'] ?? 0),
        parent_id: obj['parent_id'] || null,
        fitness: JSON.parse(obj['fitness'] ?? '{}') as Record<string, number>,
        metadata: JSON.parse(obj['metadata'] ?? '{}') as Record<string, unknown>,
        flagged_for_review: obj['flagged_for_review'] === 'true',
        created_at: obj['created_at'] ?? '',
      };
    });
  }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

/** Escape single quotes for inline SQL string values. */
function esc(s: string): string {
  return s.replace(/'/g, "''");
}
