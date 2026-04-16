/**
 * Databricks REST API client.
 *
 * Thin wrapper over fetch() for workspace APIs — SQL execution,
 * jobs, warehouses, Genie Spaces, Unity Catalog lineage.
 *
 * Auth: DATABRICKS_TOKEN env var or per-request token via setApiToken().
 */

let _currentToken: string | undefined;

export function setApiToken(token: string | undefined) {
  _currentToken = token;
}

function getConfig() {
  const rawHost = process.env.DATABRICKS_HOST?.replace(/\/$/, '');
  if (!rawHost) throw new Error('DATABRICKS_HOST env var required');
  const host = rawHost.startsWith('http') ? rawHost : `https://${rawHost}`;
  const token = _currentToken || process.env.DATABRICKS_TOKEN;
  return { host, token };
}

async function api(path: string, opts: RequestInit = {}): Promise<any> {
  const { host, token } = getConfig();
  const res = await fetch(`${host}${path}`, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Databricks API ${res.status}: ${text}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// SQL
// ---------------------------------------------------------------------------

export async function getWarehouseId(): Promise<string> {
  const data = await api('/api/2.0/sql/warehouses');
  const warehouses = data.warehouses ?? [];
  for (const wh of warehouses) {
    if (wh.warehouse_type?.toLowerCase().includes('serverless') && wh.id) return wh.id;
  }
  for (const wh of warehouses) {
    if (wh.id) return wh.id;
  }
  throw new Error('No SQL warehouse available');
}

export async function runSql(sql: string): Promise<Array<Record<string, any>>> {
  const warehouseId = await getWarehouseId();
  const data = await api('/api/2.0/sql/statements', {
    method: 'POST',
    body: JSON.stringify({ warehouse_id: warehouseId, statement: sql, wait_timeout: '30s' }),
  });
  if (data.status?.state !== 'SUCCEEDED') {
    throw new Error(`Query failed: ${JSON.stringify(data.status?.error ?? 'unknown')}`);
  }
  const cols = (data.manifest?.schema?.columns ?? []).map((c: any) => c.name);
  const rows = data.result?.data_array ?? [];
  return rows.map((r: any[]) => Object.fromEntries(cols.map((c: string, i: number) => [c, r[i]])));
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export async function listJobRuns(jobId: number, limit = 10): Promise<any[]> {
  const data = await api(`/api/2.1/jobs/runs/list?job_id=${jobId}&limit=${limit}`);
  return data.runs ?? [];
}

export async function getRunOutput(runId: number): Promise<any> {
  return api(`/api/2.1/jobs/runs/get-output?run_id=${runId}`);
}

export async function getJob(jobId: number): Promise<any> {
  return api(`/api/2.1/jobs/get?job_id=${jobId}`);
}

// ---------------------------------------------------------------------------
// Genie Spaces
// ---------------------------------------------------------------------------

export async function listGenieSpaces(): Promise<any[]> {
  const data = await api('/api/2.0/genie/spaces');
  return data.spaces ?? [];
}

export async function queryGenieSpace(spaceId: string, question: string): Promise<any> {
  const start = await api(`/api/2.0/genie/spaces/${spaceId}/conversations`, {
    method: 'POST',
    body: JSON.stringify({ content: question }),
  });
  const conversationId = start.conversation_id;
  const messageId = start.message_id;

  for (let i = 0; i < 30; i++) {
    const msg = await api(
      `/api/2.0/genie/spaces/${spaceId}/conversations/${conversationId}/messages/${messageId}`,
    );
    if (msg.status === 'COMPLETED' || msg.status === 'FAILED') return msg;
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error('Genie query timed out');
}
