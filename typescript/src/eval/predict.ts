/**
 * predict.ts — Eval bridge: create a predict function for any /responses endpoint.
 *
 * TypeScript equivalent of Python's app_predict_fn().
 */

export interface Message {
  role: string;
  content: string;
}

export interface PredictInput {
  messages: Message[];
}

export type PredictFn = (input: PredictInput | string) => Promise<string>;

export interface PredictOptions {
  /** Bearer token for Authorization header. */
  token?: string;
}

interface ResponsesPayload {
  input: string | Array<{ role: string; content: string }>;
}

interface ResponsesOutput {
  output_text?: string;
  output?: Array<{ content?: Array<{ text?: string }> }>;
}

/**
 * Create a predict function that calls a /responses endpoint.
 *
 * @param url - Base URL of the agent (e.g. "http://localhost:8000")
 * @param options - Optional config (token for auth)
 * @returns Async function that accepts messages or a plain string and returns output_text
 *
 * @example
 * const predict = createPredictFn('http://localhost:8000', { token: process.env.TOKEN });
 * const output = await predict('What is 2+2?');
 * const output2 = await predict({ messages: [{ role: 'user', content: 'Hello' }] });
 */
export function createPredictFn(url: string, options: PredictOptions = {}): PredictFn {
  const endpoint = `${url.replace(/\/$/, '')}/responses`;

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };

  if (options.token) {
    headers['Authorization'] = `Bearer ${options.token}`;
  }

  return async function predict(input: PredictInput | string): Promise<string> {
    const payload: ResponsesPayload =
      typeof input === 'string'
        ? { input }
        : { input: input.messages.map((m) => ({ role: m.role, content: m.content })) };

    const res = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`Predict request failed (${res.status}): ${text}`);
    }

    const data = (await res.json()) as ResponsesOutput;

    if (typeof data.output_text === 'string') {
      return data.output_text;
    }

    // Fallback: extract from output array
    const text = data.output?.[0]?.content?.[0]?.text;
    if (typeof text === 'string') {
      return text;
    }

    throw new Error(`Unexpected response shape: ${JSON.stringify(data)}`);
  };
}
