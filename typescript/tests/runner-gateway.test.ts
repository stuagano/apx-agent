import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';
import { runViaSDK, streamViaSDK, type RunParams } from '../src/agent/runner.js';
import { defineTool } from '../src/agent/tools.js';
import { getRequestContext, runWithContext } from '../src/agent/request-context.js';
import { resolveToken } from '../src/connectors/types.js';

const model = 'catalog.schema.authored-model';
const gateway = 'https://workspace.example.com/ai-gateway/mlflow/v1/chat/completions';
const params: RunParams = {
  model, instructions: 'Use the declared tool.', messages: [{ role: 'user', content: 'hello' }],
  tools: [], oboHeaders: { 'x-forwarded-access-token': 'request-user' },
};

async function run(stream: boolean, input = params): Promise<string[]> {
  if (!stream) return [await runViaSDK(input)];
  const chunks: string[] = [];
  for await (const chunk of streamViaSDK(input)) chunks.push(chunk);
  return chunks;
}

function sse(deltas: unknown[]): Response {
  return new Response(deltas.map((delta) => `data: ${JSON.stringify({ choices: [{ delta }] })}\n\n`).join('') + 'data: [DONE]\n\n',
    { headers: { 'Content-Type': 'text/event-stream' } });
}

beforeEach(() => {
  vi.stubEnv('DATABRICKS_HOST', 'workspace.example.com/');
  vi.stubEnv('DATABRICKS_TOKEN', 'service-token');
  vi.stubEnv('DATABRICKS_CLIENT_ID', undefined);
  vi.stubEnv('DATABRICKS_CLIENT_SECRET', undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe.each([false, true])('Gateway runner (stream=%s)', (stream) => {
  it('preserves model names, tool turns and caller context while chat uses service identity', async () => {
    const handler = vi.fn(async ({ value }: { value: string }) => ({ value, identity: await resolveToken() }));
    const tool = defineTool({
      name: 'inspect', description: 'Inspect caller identity', parameters: z.object({ value: z.string() }),
      handler,
    });
    const fetch = vi.fn().mockImplementation(async (url: string, options: RequestInit) => {
      expect(url).toBe(gateway);
      expect(new Headers(options.headers).get('Authorization')).toBe('Bearer service-token');
      const body = JSON.parse(String(options.body));
      expect(body.model).toBe(model);
      expect(body.stream).toBe(stream ? true : undefined);
      expect(body.tools[0].function.name).toBe('inspect');
      expect(body.tool_choice).toBe('auto');
      if (fetch.mock.calls.length === 1) {
        return stream ? sse([
          { tool_calls: [{ index: 0, id: 'call-1', type: 'function', function: { name: 'inspect', arguments: '{"value":' } }] },
          { tool_calls: [{ index: 0, function: { arguments: '"ok"}' } }] },
        ]) : Response.json({ choices: [{ message: { role: 'assistant', content: null,
          tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'inspect', arguments: '{"value":"ok"}' } }] } }] });
      }
      expect(body.messages.at(-1)).toEqual({ role: 'tool', tool_call_id: 'call-1',
        content: JSON.stringify({ value: 'ok', identity: 'request-user' }) });
      return stream ? sse([{ content: 'Hello ' }, { content: 'world' }])
        : Response.json({ choices: [{ message: { role: 'assistant', content: 'Hello world' } }] });
    });
    vi.stubGlobal('fetch', fetch);
    await runWithContext({ oboHeaders: { authorization: 'Bearer ambient-user' } }, async () => {
      expect(await run(stream, { ...params, tools: [tool] })).toEqual(stream ? ['Hello ', 'world'] : ['Hello world']);
      expect(getRequestContext()?.oboHeaders.authorization).toBe('Bearer ambient-user');
    });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('does not fall back to caller credentials when service credentials are missing', async () => {
    vi.stubEnv('DATABRICKS_TOKEN', undefined);
    const fetch = vi.fn(async () => { throw new Error('Unexpected model request without service credentials'); });
    vi.stubGlobal('fetch', fetch);
    await expect(runWithContext({ oboHeaders: params.oboHeaders }, () => run(stream)))
      .rejects.toThrow('AI Gateway chat requires service credentials');
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([401, 403])('reports actionable authorization failure for HTTP %s', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('private backend detail', { status })));
    await expect(run(stream)).rejects.toThrow(`AI Gateway ${status}: check service credentials and access to model service`);
  });
});

it('reuses M2M credentials for chat without inheriting caller OBO', async () => {
  vi.stubEnv('DATABRICKS_TOKEN', undefined);
  vi.stubEnv('DATABRICKS_CLIENT_ID', 'test-client');
  vi.stubEnv('DATABRICKS_CLIENT_SECRET', 'test-client-secret');
  const fetch = vi.fn(async (url: string, options: RequestInit) => {
    if (url.endsWith('/oidc/v1/token')) {
      expect(url).toBe('https://workspace.example.com/oidc/v1/token');
      expect(new URLSearchParams(String(options.body)).get('grant_type')).toBe('client_credentials');
      return Response.json({ access_token: 'test-service-oauth', expires_in: 3600 });
    }
    expect(url).toBe(gateway);
    expect(new Headers(options.headers).get('Authorization')).toBe('Bearer test-service-oauth');
    const body = JSON.parse(String(options.body));
    return body.stream ? sse([{ content: 'ok' }])
      : Response.json({ choices: [{ message: { role: 'assistant', content: 'ok' } }] });
  });
  vi.stubGlobal('fetch', fetch);
  await runWithContext({ oboHeaders: params.oboHeaders }, async () => {
    expect(await run(false)).toEqual(['ok']);
    expect(await run(true)).toEqual(['ok']);
    expect(await resolveToken()).toBe('request-user');
  });
  expect(fetch).toHaveBeenCalledTimes(3);
});
