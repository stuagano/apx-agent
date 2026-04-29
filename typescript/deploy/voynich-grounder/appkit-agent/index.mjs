import { zodToJsonSchema as zodToJsonSchema$1 } from "zod-to-json-schema";
import { AsyncLocalStorage } from "node:async_hooks";
import { z } from "zod";
import { randomUUID } from "node:crypto";

//#region src/agent/tools.ts
/**
* Define a typed agent tool.
*
* @example
* const getLineage = defineTool({
*   name: 'get_table_lineage',
*   description: 'Get upstream sources for a table',
*   parameters: z.object({ tableName: z.string() }),
*   handler: async ({ tableName }) => {
*     // query Unity Catalog lineage
*   },
* });
*/
function defineTool(opts) {
	return {
		name: opts.name,
		description: opts.description,
		parameters: opts.parameters,
		handler: async (raw) => {
			const parsed = opts.parameters.parse(raw);
			return opts.handler(parsed);
		}
	};
}
/** Convert a Zod schema to JSON Schema, suitable for OpenAI function calling. */
function zodToJsonSchema(schema) {
	if ("toJSONSchema" in schema && typeof schema.toJSONSchema === "function") return schema.toJSONSchema();
	try {
		return zodToJsonSchema$1(schema, { target: "openAi" });
	} catch {
		return {
			type: "object",
			properties: {}
		};
	}
}
/**
* Ensure a JSON schema is "strict" for OpenAI — adds `additionalProperties: false`
* on all object types recursively.
*/
function toStrictSchema(schema) {
	if (!schema) return {
		type: "object",
		properties: {},
		required: [],
		additionalProperties: false
	};
	const result = { ...schema };
	delete result["$schema"];
	if (result.type === "object") {
		result.additionalProperties = false;
		if (!result.required) result.required = Object.keys(result.properties ?? {});
		if (result.properties && typeof result.properties === "object") result.properties = Object.fromEntries(Object.entries(result.properties).map(([k, v]) => {
			if (typeof v === "object" && v !== null && v.type === "object") return [k, toStrictSchema(v)];
			return [k, v];
		}));
	}
	return result;
}
/** Convert AgentTools to OpenAI function calling format. */
function toolsToFunctionSchemas(tools) {
	return tools.map((t) => ({
		type: "function",
		function: {
			name: t.name,
			description: t.description,
			parameters: toStrictSchema(zodToJsonSchema(t.parameters))
		}
	}));
}

//#endregion
//#region src/agent/request-context.ts
/**
* Per-request context propagated via AsyncLocalStorage.
*
* The runner sets this before calling each tool handler so that tools
* (genieTool, connector tools, etc.) can transparently access OBO auth
* headers without needing to receive them as explicit arguments.
*/
const storage = new AsyncLocalStorage();
/** Run `fn` with the given context available to all async descendants. */
function runWithContext(ctx, fn) {
	return storage.run(ctx, fn);
}
/** Return the current request context, or undefined outside a request. */
function getRequestContext() {
	return storage.getStore();
}

//#endregion
//#region src/trace.ts
const MAX_TRACES = 200;
const traceBuffer = [];
function storeTrace(trace) {
	traceBuffer.push(trace);
	if (traceBuffer.length > MAX_TRACES) traceBuffer.shift();
}
function getTraces() {
	return [...traceBuffer].reverse();
}
function getTrace(id) {
	return traceBuffer.find((t) => t.id === id);
}
let idCounter = 0;
function createTrace(agentName) {
	return {
		id: `tr-${Date.now()}-${++idCounter}`,
		agentName,
		startTime: Date.now(),
		spans: [],
		status: "in_progress"
	};
}
function addSpan(trace, span) {
	const full = {
		...span,
		startTime: Date.now()
	};
	trace.spans.push(full);
	return full;
}
function endSpan(span) {
	span.duration_ms = Date.now() - span.startTime;
}
function endTrace(trace, status = "completed") {
	trace.endTime = Date.now();
	trace.duration_ms = trace.endTime - trace.startTime;
	trace.status = status;
	storeTrace(trace);
}
function truncate(value, maxLen = 200) {
	const s = typeof value === "string" ? value : JSON.stringify(value);
	if (!s) return "";
	return s.length > maxLen ? s.slice(0, maxLen) + "..." : s;
}

//#endregion
//#region src/connectors/types.ts
const fieldDefSchema = z.object({
	name: z.string(),
	type: z.string(),
	key: z.boolean().optional(),
	nullable: z.boolean().optional(),
	default: z.union([z.number(), z.string()]).optional(),
	index: z.boolean().optional()
});
const entityDefSchema = z.object({
	name: z.string(),
	table: z.string(),
	fields: z.array(fieldDefSchema),
	embedding_source: z.string().optional()
});
const edgeDefSchema = z.object({
	name: z.string(),
	table: z.string(),
	from: z.string(),
	to: z.string(),
	fields: z.array(fieldDefSchema)
});
const extractionSchema = z.object({
	prompt_template: z.string(),
	chunk_size: z.number().int().min(1),
	chunk_overlap: z.number().int().min(0)
});
const fitnessSchema = z.object({
	metric: z.string(),
	evaluation: z.string(),
	targets: z.record(z.string(), z.number())
});
const evolutionSchema = z.object({
	population_size: z.number().int().min(1),
	mutation_rate: z.number().min(0).max(1),
	mutation_fields: z.array(z.string()),
	selection: z.string(),
	max_generations: z.number().int().min(1)
});
const entitySchemaValidator = z.object({
	version: z.number().int(),
	generation: z.number().int().min(0),
	entities: z.array(entityDefSchema),
	edges: z.array(edgeDefSchema),
	extraction: extractionSchema,
	fitness: fitnessSchema,
	evolution: evolutionSchema
});
function parseEntitySchema(raw) {
	return entitySchemaValidator.parse(raw);
}
let m2mToken = null;
let m2mExpiry = 0;
let m2mInFlight = null;
/**
* Exchange DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET for an OAuth
* access token via the Databricks OIDC token endpoint. The token is cached
* and refreshed 60 seconds before expiry.
*
* This is the standard OAuth 2.0 client_credentials grant — the same flow
* that Databricks Jobs, Workflows, and service principals use.
*/
async function acquireM2mToken() {
	const clientId = process.env.DATABRICKS_CLIENT_ID;
	const clientSecret = process.env.DATABRICKS_CLIENT_SECRET;
	if (!clientId || !clientSecret) throw new Error("No Databricks token available. Provide one of:\n  - X-Forwarded-Access-Token header (interactive/OBO)\n  - DATABRICKS_TOKEN env var (static PAT)\n  - DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET (M2M OAuth)");
	const tokenUrl = `${resolveHost()}/oidc/v1/token`;
	const res = await fetch(tokenUrl, {
		method: "POST",
		headers: { "Content-Type": "application/x-www-form-urlencoded" },
		body: new URLSearchParams({
			grant_type: "client_credentials",
			client_id: clientId,
			client_secret: clientSecret,
			scope: "all-apis"
		})
	});
	if (!res.ok) {
		const text = await res.text();
		throw new Error(`M2M token exchange failed (${res.status}): ${text}`);
	}
	const data = await res.json();
	m2mToken = data.access_token;
	m2mExpiry = Date.now() + ((data.expires_in ?? 3600) - 60) * 1e3;
	return m2mToken;
}
/**
* Get a cached M2M token, refreshing if expired. Deduplicates concurrent
* requests so only one token exchange is in-flight at a time.
*/
async function getM2mToken() {
	if (m2mToken && Date.now() < m2mExpiry) return m2mToken;
	if (!m2mInFlight) m2mInFlight = acquireM2mToken().finally(() => {
		m2mInFlight = null;
	});
	return m2mInFlight;
}
/**
* Resolve a Databricks bearer token for an outbound API call.
*
* Priority order — checked at call time so per-request OBO tokens are
* always used when available, not captured at construction time:
*   1. Explicit `oboHeaders` argument (e.g. passed from the incoming request)
*   2. `AsyncLocalStorage` request context — set by the agent framework for
*      every tool handler and sub-agent call; reads `x-forwarded-access-token`
*      or `authorization` from the user's OBO headers
*   3. `DATABRICKS_TOKEN` env var — static PAT for local dev
*   4. M2M OAuth via `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` —
*      service principal identity for jobs, workflows, and background loops
*
* Steps 1-3 are synchronous. Step 4 requires an async token exchange, so
* this function returns `string | Promise<string>`. All call sites already
* run in async handlers, so `await resolveToken()` works everywhere.
*/
async function resolveToken(oboHeaders) {
	if (oboHeaders) {
		const auth = oboHeaders["authorization"] ?? oboHeaders["Authorization"];
		if (auth?.startsWith("Bearer ")) return auth.slice(7);
		const xfat = oboHeaders["x-forwarded-access-token"];
		if (xfat) return xfat;
	}
	const ctx = getRequestContext();
	if (ctx) {
		const token = ctx.oboHeaders["x-forwarded-access-token"] || (ctx.oboHeaders["authorization"] ?? "").replace(/^Bearer\s+/i, "");
		if (token) return token;
	}
	const envToken = process.env.DATABRICKS_TOKEN;
	if (envToken) return envToken;
	if (process.env.DATABRICKS_CLIENT_ID && process.env.DATABRICKS_CLIENT_SECRET) {
		if (m2mToken && Date.now() < m2mExpiry) return m2mToken;
		return getM2mToken();
	}
	throw new Error("No Databricks token available. Provide one of:\n  - X-Forwarded-Access-Token header (interactive/OBO)\n  - DATABRICKS_TOKEN env var (static PAT)\n  - DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET (M2M OAuth)");
}
function resolveHost(host) {
	const h = host ?? process.env.DATABRICKS_HOST;
	if (!h) throw new Error("No Databricks host: pass host in config or set DATABRICKS_HOST env var");
	return (h.startsWith("http") ? h : `https://${h}`).replace(/\/$/, "");
}
function buildSqlParams(filters) {
	const entries = Object.entries(filters);
	if (entries.length === 0) return {
		clause: "",
		params: []
	};
	const params = entries.map(([key, value]) => {
		let type = "STRING";
		if (typeof value === "number") type = Number.isInteger(value) ? "INT" : "FLOAT";
		else if (typeof value === "boolean") type = "BOOLEAN";
		return {
			name: key,
			value: String(value),
			type
		};
	});
	return {
		clause: entries.map(([key]) => `${key} = :${key}`).join(" AND "),
		params
	};
}
async function dbFetch(url, opts) {
	const headers = { Authorization: `Bearer ${opts.token}` };
	const init = {
		method: opts.method,
		headers
	};
	if (opts.body !== void 0) {
		headers["Content-Type"] = "application/json";
		init.body = JSON.stringify(opts.body);
	}
	const res = await fetch(url, init);
	if (!res.ok) {
		const text = await res.text();
		throw new Error(`Databricks API ${res.status}: ${text}`);
	}
	return res.json();
}

//#endregion
//#region src/agent/runner.ts
function getHost() {
	const host = process.env.DATABRICKS_HOST;
	if (!host) throw new Error("DATABRICKS_HOST env var required");
	return host.startsWith("http") ? host.replace(/\/$/, "") : `https://${host}`;
}
/**
* Resolve auth token for FMAPI calls.
*
* For FMAPI (model serving), the app should use its own identity — NOT the
* caller's OBO token, which may be another app's SP that lacks FMAPI access.
*
* Priority: DATABRICKS_TOKEN env → M2M OAuth (app's own SP credentials).
* OBO headers are intentionally skipped here; they are used for data
* operations (UC, SQL) where the caller's identity matters.
*/
function resolveToken$1(_oboHeaders) {
	try {
		return resolveToken();
	} catch {
		return;
	}
}
function fetchWithTimeout(url, opts, timeoutMs) {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	return fetch(url, {
		...opts,
		signal: controller.signal
	}).finally(() => clearTimeout(timer));
}
async function chatCompletions(model, messages, token, tools) {
	const host = getHost();
	const body = {
		model,
		messages
	};
	if (tools && tools.length > 0) {
		body.tools = tools;
		body.tool_choice = "auto";
	}
	const ctx = getRequestContext();
	const span = ctx?.trace ? addSpan(ctx.trace, {
		type: "llm",
		name: model,
		input: truncate(messages),
		metadata: {
			model,
			tool_count: tools?.length ?? 0
		}
	}) : null;
	try {
		const res = await fetchWithTimeout(`${host}/serving-endpoints/chat/completions`, {
			method: "POST",
			headers: {
				"Content-Type": "application/json",
				...token ? { Authorization: `Bearer ${token}` } : {}
			},
			body: JSON.stringify(body)
		}, 12e4);
		if (!res.ok) {
			const text = await res.text();
			throw new Error(`FMAPI ${res.status}: ${text}`);
		}
		const result = await res.json();
		if (span) {
			span.output = truncate(result);
			endSpan(span);
		}
		return result;
	} catch (err) {
		if (span) {
			span.output = String(err);
			span.metadata = {
				...span.metadata,
				error: true
			};
			endSpan(span);
		}
		throw err;
	}
}
function toToolDef(tool) {
	const params = toStrictSchema(zodToJsonSchema(tool.parameters));
	return {
		type: "function",
		function: {
			name: tool.name,
			description: tool.description,
			parameters: params
		}
	};
}
function subAgentToolDef(name, description) {
	return {
		type: "function",
		function: {
			name,
			description,
			parameters: {
				type: "object",
				properties: { message: {
					type: "string",
					description: "The message to send to the agent"
				} },
				required: ["message"],
				additionalProperties: false
			}
		}
	};
}
async function callSubAgent(url, message, oboHeaders) {
	const res = await fetch(`${url.replace(/\/$/, "")}/responses`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			...oboHeaders
		},
		body: JSON.stringify({ input: [{
			role: "user",
			content: message
		}] })
	});
	if (!res.ok) return `Sub-agent error (${res.status}): ${await res.text()}`;
	const data = await res.json();
	if (data.output_text && typeof data.output_text === "string") return data.output_text;
	const output = data.output;
	if (output?.[0]?.content?.[0]?.text) return output[0].content[0].text;
	return JSON.stringify(data);
}
/**
* @deprecated FMAPI runner handles auth internally. This is a no-op kept
* for backward compatibility with plugin.ts setup().
*/
function initDatabricksClient() {
	getHost();
}
/**
* @deprecated Kept for backward compatibility. Tools are called directly now.
*/
function toFunctionTool(agentTool, ..._rest) {
	return {
		name: agentTool.name,
		handler: agentTool.handler
	};
}
/**
* @deprecated Kept for backward compatibility.
*/
function toSubAgentTool(name, description, url, oboHeaders) {
	return {
		name,
		execute: async (args) => callSubAgent(url, args.message ?? JSON.stringify(args), oboHeaders)
	};
}
/** Run the agent loop and return the final text. */
async function runViaSDK(params) {
	const token = await resolveToken$1(params.oboHeaders);
	const toolMap = new Map(params.tools.map((t) => [t.name, t]));
	const subAgentMap = new Map((params.subAgents ?? []).map((url, i) => [`sub_agent_${i}`, url]));
	const toolDefs = [...params.tools.map(toToolDef), ...(params.subAgents ?? []).map((url, i) => subAgentToolDef(`sub_agent_${i}`, `Remote agent at ${url}`))];
	const messages = [{
		role: "system",
		content: params.instructions || "You are a helpful assistant."
	}, ...params.messages.map((m) => ({
		role: m.role,
		content: m.content
	}))];
	const maxTurns = params.maxTurns ?? 10;
	for (let turn = 0; turn < maxTurns; turn++) {
		const choice = (await chatCompletions(params.model, messages, token, toolDefs.length > 0 ? toolDefs : void 0)).choices?.[0];
		if (!choice) return "";
		const assistantMsg = choice.message;
		messages.push(assistantMsg);
		if (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0) return assistantMsg.content ?? "";
		for (const tc of assistantMsg.tool_calls) {
			let result;
			const tool = toolMap.get(tc.function.name);
			const subAgentUrl = subAgentMap.get(tc.function.name);
			const ctx = getRequestContext();
			const toolSpan = ctx?.trace ? addSpan(ctx.trace, {
				type: "tool",
				name: tc.function.name,
				input: truncate(tc.function.arguments)
			}) : null;
			if (tool) try {
				const args = JSON.parse(tc.function.arguments);
				const output = await runWithContext({
					oboHeaders: params.oboHeaders,
					trace: ctx?.trace
				}, () => tool.handler(args));
				result = typeof output === "string" ? output : JSON.stringify(output);
			} catch (e) {
				result = `Tool error: ${e instanceof Error ? e.message : String(e)}`;
			}
			else if (subAgentUrl) try {
				const args = JSON.parse(tc.function.arguments);
				result = await callSubAgent(subAgentUrl, args.message ?? JSON.stringify(args), params.oboHeaders);
			} catch (e) {
				result = `Sub-agent error: ${e instanceof Error ? e.message : String(e)}`;
			}
			else result = `Tool not found: ${tc.function.name}`;
			if (toolSpan) {
				toolSpan.output = truncate(result);
				endSpan(toolSpan);
			}
			messages.push({
				role: "tool",
				content: result,
				tool_call_id: tc.id
			});
		}
	}
	return [...messages].reverse().find((m) => m.role === "assistant")?.content ?? "[Max tool-calling turns exceeded]";
}
/** Stream the agent loop, yielding text chunks. */
async function* streamViaSDK(params) {
	const token = await resolveToken$1(params.oboHeaders);
	const toolMap = new Map(params.tools.map((t) => [t.name, t]));
	const subAgentMap = new Map((params.subAgents ?? []).map((url, i) => [`sub_agent_${i}`, url]));
	const toolDefs = [...params.tools.map(toToolDef), ...(params.subAgents ?? []).map((url, i) => subAgentToolDef(`sub_agent_${i}`, `Remote agent at ${url}`))];
	const messages = [{
		role: "system",
		content: params.instructions || "You are a helpful assistant."
	}, ...params.messages.map((m) => ({
		role: m.role,
		content: m.content
	}))];
	const maxTurns = params.maxTurns ?? 10;
	for (let turn = 0; turn < maxTurns; turn++) {
		const choice = (await chatCompletions(params.model, messages, token, toolDefs.length > 0 ? toolDefs : void 0)).choices?.[0];
		if (!choice) return;
		const assistantMsg = choice.message;
		messages.push(assistantMsg);
		if (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0) {
			if (assistantMsg.content) yield assistantMsg.content;
			return;
		}
		for (const tc of assistantMsg.tool_calls) {
			let result;
			const tool = toolMap.get(tc.function.name);
			const subAgentUrl = subAgentMap.get(tc.function.name);
			const ctx = getRequestContext();
			const toolSpan = ctx?.trace ? addSpan(ctx.trace, {
				type: "tool",
				name: tc.function.name,
				input: truncate(tc.function.arguments)
			}) : null;
			if (tool) try {
				const args = JSON.parse(tc.function.arguments);
				const output = await runWithContext({
					oboHeaders: params.oboHeaders,
					trace: ctx?.trace
				}, () => tool.handler(args));
				result = typeof output === "string" ? output : JSON.stringify(output);
			} catch (e) {
				result = `Tool error: ${e instanceof Error ? e.message : String(e)}`;
			}
			else if (subAgentUrl) try {
				const args = JSON.parse(tc.function.arguments);
				result = await callSubAgent(subAgentUrl, args.message ?? JSON.stringify(args), params.oboHeaders);
			} catch (e) {
				result = `Sub-agent error: ${e instanceof Error ? e.message : String(e)}`;
			}
			else result = `Tool not found: ${tc.function.name}`;
			if (toolSpan) {
				toolSpan.output = truncate(result);
				endSpan(toolSpan);
			}
			messages.push({
				role: "tool",
				content: result,
				tool_call_id: tc.id
			});
		}
	}
	yield "[Max tool-calling turns exceeded]";
}

//#endregion
//#region src/agent/mcp-client.ts
/**
* MCP client — consume remote MCP servers as AgentTools.
*
* Connects to external MCP endpoints via StreamableHTTP, discovers their
* tool manifests, and wraps each tool as an AgentTool so it can be passed
* directly into the agent plugin's tool list.
*
* Supports Databricks managed MCP URLs:
*   /api/2.0/mcp/genie/{space_id}          — Genie Space
*   /api/2.0/mcp/functions/{catalog}/{schema} — UC Functions
*
* Usage:
*   const genieTools = await discoverMcpTools(
*     'https://my-workspace.databricks.com/api/2.0/mcp/genie/abc123',
*     { token: process.env.DATABRICKS_TOKEN! },
*   );
*
*   const allTools = await createMcpToolProvider([
*     'https://my-workspace.databricks.com/api/2.0/mcp/genie/abc123',
*     'https://my-workspace.databricks.com/api/2.0/mcp/functions/main/default',
*   ]);
*/
/**
* Convert a JSON Schema object (as returned by MCP tools/list) into a Zod schema.
*
* MCP inputSchema is always `{ type: 'object', properties: {...}, required: [...] }`.
* We convert it structurally so that the AgentTool's parameter validation works,
* and so downstream consumers (OpenAI function calling, MCP server re-export) get
* the correct shape.
*/
function jsonSchemaToZod(schema) {
	const type = schema.type;
	if (type === "object") {
		const properties = schema.properties ?? {};
		const required = schema.required ?? [];
		const shape = {};
		for (const [key, propSchema] of Object.entries(properties)) {
			const zodProp = jsonSchemaToZod(propSchema);
			shape[key] = required.includes(key) ? zodProp : zodProp.optional();
		}
		return z.object(shape);
	}
	if (type === "array") {
		const items = schema.items ?? {};
		return z.array(jsonSchemaToZod(items));
	}
	if (type === "string") {
		let s = z.string();
		if (schema.description) s = s.describe(schema.description);
		if (schema.enum) {
			const values = schema.enum;
			return z.enum(values);
		}
		return s;
	}
	if (type === "number" || type === "integer") {
		let n = z.number();
		if (type === "integer") n = n.int();
		return n;
	}
	if (type === "boolean") return z.boolean();
	if (Array.isArray(type)) {
		if (type.includes("null") && type.length === 2) {
			const nonNull = type.find((t) => t !== "null");
			return jsonSchemaToZod({
				...schema,
				type: nonNull
			}).nullable();
		}
		return z.unknown();
	}
	return z.unknown();
}
function extractToolResultText(result) {
	if (typeof result === "string") return result;
	const r = result;
	if (r.content && Array.isArray(r.content)) return r.content.filter((c) => c.type === "text" && c.text).map((c) => c.text).join("\n") || JSON.stringify(result);
	if (r.toolResult !== void 0) return typeof r.toolResult === "string" ? r.toolResult : JSON.stringify(r.toolResult);
	return JSON.stringify(result);
}
/**
* Connect to a remote MCP endpoint, call tools/list, and return an AgentTool
* for each discovered tool.
*
* Each returned AgentTool's handler:
* 1. Opens a fresh MCP client connection (stateless — matches Databricks managed MCP behavior)
* 2. Calls the tool via the client
* 3. Closes the connection
*
* @param url  Full URL of the MCP endpoint (e.g. https://host/api/2.0/mcp/genie/abc)
* @param auth Optional bearer token for authenticated endpoints
*/
async function discoverMcpTools(url, auth) {
	const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
	const { StreamableHTTPClientTransport } = await import("@modelcontextprotocol/sdk/client/streamableHttp.js");
	let discoveryToken;
	try {
		discoveryToken = await resolveToken(auth ? { authorization: `Bearer ${auth.token}` } : void 0);
	} catch {}
	const requestInit = discoveryToken ? { headers: { Authorization: `Bearer ${discoveryToken}` } } : {};
	const discoverClient = new Client({
		name: "appkit-agent-discovery",
		version: "1.0.0"
	}, { capabilities: {} });
	const discoverTransport = new StreamableHTTPClientTransport(new URL(url), { requestInit });
	try {
		await discoverClient.connect(discoverTransport);
	} catch (err) {
		throw new Error(`Failed to connect to MCP server at ${url}: ${err instanceof Error ? err.message : String(err)}`);
	}
	let mcpTools;
	try {
		mcpTools = (await discoverClient.listTools()).tools;
	} finally {
		await discoverClient.close();
	}
	return mcpTools.map((mcpTool) => {
		const parameters = jsonSchemaToZod(mcpTool.inputSchema ?? {
			type: "object",
			properties: {}
		});
		return {
			name: mcpTool.name,
			description: mcpTool.description ?? mcpTool.name,
			parameters,
			handler: async (args) => {
				const callClient = new Client({
					name: "appkit-agent",
					version: "1.0.0"
				}, { capabilities: {} });
				let callToken;
				try {
					callToken = await resolveToken(auth ? { authorization: `Bearer ${auth.token}` } : void 0);
				} catch {}
				const callRequestInit = callToken ? { headers: { Authorization: `Bearer ${callToken}` } } : {};
				const callTransport = new StreamableHTTPClientTransport(new URL(url), { requestInit: callRequestInit });
				try {
					await callClient.connect(callTransport);
					return extractToolResultText(await callClient.callTool({
						name: mcpTool.name,
						arguments: args ?? {}
					}));
				} finally {
					await callClient.close();
				}
			}
		};
	});
}
/**
* Discover tools from multiple MCP server URLs and return a combined AgentTool[].
*
* Connects to all servers in parallel. If a single server fails discovery,
* a warning is emitted and discovery continues with the remaining servers.
*
* @param urls  Array of MCP server URLs
* @param auth  Optional shared bearer token (applies to all URLs)
*/
async function createMcpToolProvider(urls, auth) {
	const results = await Promise.allSettled(urls.map((url) => discoverMcpTools(url, auth)));
	const tools = [];
	for (let i = 0; i < results.length; i++) {
		const result = results[i];
		if (result.status === "fulfilled") tools.push(...result.value);
		else console.warn(`[mcp-client] Failed to discover tools from ${urls[i]}:`, result.reason);
	}
	return tools;
}

//#endregion
//#region src/agent/plugin.ts
function getOboHeaders(req) {
	return {
		authorization: req.headers.authorization ?? "",
		"x-forwarded-access-token": req.headers["x-forwarded-access-token"] ?? "",
		"x-forwarded-host": req.headers["x-forwarded-host"] ?? "",
		"x-forwarded-user": req.headers["x-forwarded-user"] ?? ""
	};
}
function parseInput(raw) {
	const input = raw.input;
	if (typeof input === "string") return [{
		role: "user",
		content: input
	}];
	return input.map((item) => {
		const role = item.role ?? "user";
		let content = item.content ?? "";
		if (Array.isArray(content)) content = content.filter((p) => p.type === "input_text" || p.type === "text").map((p) => p.text ?? "").join(" ");
		return {
			role,
			content: String(content)
		};
	});
}
/**
* Create the agent plugin.
*
* This returns a plain plugin object compatible with AppKit's createApp().
* When AppKit's class-based Plugin API is confirmed and stable, this can
* be converted to extend Plugin<AgentConfig>.
*/
function createAgentPlugin(config) {
	const tools = [...config.tools ?? []];
	const toolMap = new Map(tools.map((t) => [t.name, t]));
	const apiPrefix = config.apiPrefix ?? "/api/agent";
	let app;
	return {
		name: "agent",
		displayName: "Agent Plugin",
		description: "AI agent with typed tools and deterministic routing",
		async setup(expressApp) {
			app = expressApp;
			initDatabricksClient();
			if (config.mcpServers && config.mcpServers.length > 0) try {
				const mcpTools = await createMcpToolProvider(config.mcpServers);
				for (const mcpTool of mcpTools) if (!toolMap.has(mcpTool.name)) {
					tools.push(mcpTool);
					toolMap.set(mcpTool.name, mcpTool);
				} else console.warn(`[agent] MCP tool "${mcpTool.name}" conflicts with an existing tool — skipping`);
			} catch (err) {
				console.warn("[agent] MCP tool discovery failed:", err instanceof Error ? err.message : String(err));
			}
		},
		injectRoutes(router) {
			const healthHandler = (_req, res) => {
				res.json({ status: "ok" });
			};
			router.get(`${apiPrefix}/health`, healthHandler);
			router.get("/api/health", healthHandler);
			for (const tool of tools) router.post(`${apiPrefix}/tools/${tool.name}`, async (req, res) => {
				try {
					const result = await runWithContext({ oboHeaders: getOboHeaders(req) }, () => tool.handler(req.body));
					res.json(result);
				} catch (err) {
					const message = err instanceof Error ? err.message : String(err);
					res.status(500).json({ error: message });
				}
			});
			const responsesHandler = async (req, res) => {
				const trace = createTrace(config.name ?? "agent");
				addSpan(trace, {
					type: "request",
					name: "POST /responses",
					input: truncate(req.body)
				});
				try {
					const raw = req.body;
					const messages = parseInput(raw);
					const oboHeaders = getOboHeaders(req);
					if (config.workflow) {
						const workflowMessages = messages.map((m) => ({
							role: m.role,
							content: m.content
						}));
						if (raw.stream && config.workflow.stream) {
							res.setHeader("Content-Type", "text/event-stream");
							res.setHeader("Cache-Control", "no-cache");
							res.setHeader("Connection", "keep-alive");
							const itemId = "msg_001";
							res.write(`event: response.output_item.start\ndata: ${JSON.stringify({ item_id: itemId })}\n\n`);
							let fullText = "";
							try {
								await runWithContext({
									oboHeaders,
									trace
								}, async () => {
									for await (const chunk of config.workflow.stream(workflowMessages)) {
										fullText += chunk;
										res.write(`event: output_text.delta\ndata: ${JSON.stringify({
											item_id: itemId,
											text: chunk
										})}\n\n`);
									}
								});
								const output = {
									type: "message",
									role: "assistant",
									content: [{
										type: "output_text",
										text: fullText
									}]
								};
								res.write(`event: response.output_item.done\ndata: ${JSON.stringify({
									item_id: itemId,
									output
								})}\n\n`);
								addSpan(trace, {
									type: "response",
									name: "response",
									output: truncate(fullText)
								});
								endTrace(trace);
							} catch (err) {
								const errMsg = err instanceof Error ? err.message : String(err);
								res.write(`event: error\ndata: ${JSON.stringify({
									item_id: itemId,
									error: errMsg
								})}\n\n`);
								endTrace(trace, "error");
							}
							res.end();
							return;
						}
						const text = await runWithContext({
							oboHeaders,
							trace
						}, () => config.workflow.run(workflowMessages));
						const response = {
							id: `resp_${Date.now()}`,
							object: "response",
							status: "completed",
							output: [{
								type: "message",
								role: "assistant",
								content: [{
									type: "output_text",
									text
								}]
							}],
							output_text: text
						};
						addSpan(trace, {
							type: "response",
							name: "response",
							output: truncate(text)
						});
						endTrace(trace);
						res.json(response);
						return;
					}
					if (raw.stream) {
						res.setHeader("Content-Type", "text/event-stream");
						res.setHeader("Cache-Control", "no-cache");
						res.setHeader("Connection", "keep-alive");
						const itemId = "msg_001";
						res.write(`event: response.output_item.start\ndata: ${JSON.stringify({ item_id: itemId })}\n\n`);
						let fullText = "";
						const heartbeat = setInterval(() => {
							res.write(": keepalive\n\n");
						}, 15e3);
						try {
							await runWithContext({
								oboHeaders,
								trace
							}, async () => {
								for await (const chunk of streamViaSDK({
									model: config.model,
									instructions: config.instructions ?? "",
									messages,
									tools,
									subAgents: config.subAgents,
									maxTurns: config.maxIterations,
									app,
									oboHeaders,
									apiPrefix
								})) {
									fullText += chunk;
									res.write(`event: output_text.delta\ndata: ${JSON.stringify({
										item_id: itemId,
										text: chunk
									})}\n\n`);
								}
							});
							const output = {
								type: "message",
								role: "assistant",
								content: [{
									type: "output_text",
									text: fullText
								}]
							};
							res.write(`event: response.output_item.done\ndata: ${JSON.stringify({
								item_id: itemId,
								output
							})}\n\n`);
							addSpan(trace, {
								type: "response",
								name: "response",
								output: truncate(fullText)
							});
							endTrace(trace);
						} catch (err) {
							const message = err instanceof Error ? err.message : String(err);
							res.write(`event: error\ndata: ${JSON.stringify({
								item_id: itemId,
								error: message
							})}\n\n`);
							endTrace(trace, "error");
						} finally {
							clearInterval(heartbeat);
						}
						res.end();
						return;
					}
					const text = await runWithContext({
						oboHeaders,
						trace
					}, () => runViaSDK({
						model: config.model,
						instructions: config.instructions ?? "",
						messages,
						tools,
						subAgents: config.subAgents,
						maxTurns: config.maxIterations,
						app,
						oboHeaders,
						apiPrefix
					}));
					const response = {
						id: `resp_${Date.now()}`,
						object: "response",
						status: "completed",
						output: [{
							type: "message",
							role: "assistant",
							content: [{
								type: "output_text",
								text
							}]
						}],
						output_text: text
					};
					addSpan(trace, {
						type: "response",
						name: "response",
						output: truncate(text)
					});
					endTrace(trace);
					res.json(response);
				} catch (err) {
					const message = err instanceof Error ? err.message : String(err);
					endTrace(trace, "error");
					res.status(500).json({ error: message });
				}
			};
			router.post("/responses", responsesHandler);
			router.post("/api/responses", responsesHandler);
		},
		exports() {
			return {
				getTools: () => tools,
				getConfig: () => config,
				getToolSchemas: () => toolsToFunctionSchemas(tools)
			};
		}
	};
}

//#endregion
//#region src/discovery/index.ts
function resolveEnvVar(value) {
	if (!value.startsWith("$")) return value;
	const varName = value.replace(/^\$\{?/, "").replace(/\}$/, "");
	return process.env[varName] ?? "";
}
function createDiscoveryPlugin(config, agentExports) {
	return {
		name: "discovery",
		displayName: "Agent Discovery",
		description: "A2A agent card and registry auto-registration",
		setup() {
			if (config.registry) {
				const registryUrl = resolveEnvVar(config.registry);
				const publicUrl = config.url ? resolveEnvVar(config.url) : "";
				if (registryUrl) setTimeout(() => registerWithHub(registryUrl, publicUrl), 2e3);
			}
		},
		injectRoutes(router) {
			router.get("/.well-known/agent.json", (req, res) => {
				const exports = agentExports();
				const tools = exports?.getTools() ?? [];
				const agentConfig = exports?.getConfig();
				const baseUrl = `${req.protocol}://${req.get("host")}`;
				const card = {
					schemaVersion: "1.0",
					name: config.name ?? agentConfig?.model ?? "agent",
					description: config.description ?? "",
					url: baseUrl,
					protocolVersion: "0.3.0",
					capabilities: {
						streaming: true,
						multiTurn: true
					},
					authentication: {
						schemes: ["bearer"],
						credentials: "same_origin"
					},
					skills: tools.map((t) => ({
						id: t.name,
						name: t.name,
						description: t.description
					})),
					mcpEndpoint: `${baseUrl}/mcp`
				};
				res.json(card);
			});
		}
	};
}
async function registerWithHub(registryUrl, publicUrl) {
	try {
		const url = registryUrl.replace(/\/$/, "");
		const body = {};
		if (publicUrl) body.url = publicUrl.replace(/\/$/, "");
		const response = await fetch(`${url}/api/agents/register`, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(body)
		});
		if (!response.ok) {
			console.warn(`Registry registration failed: ${response.status}`);
			return;
		}
		const data = await response.json();
		console.log(`Registered with agent registry at ${url} as '${data.id ?? "unknown"}'`);
	} catch (err) {
		console.warn(`Failed to register with agent registry:`, err);
	}
}

//#endregion
//#region src/mcp/index.ts
/**
* MCP server plugin for Databricks AppKit.
*
* Exposes the agent's tools as an MCP server so Supervisor Agent,
* Claude Desktop, Cursor, and Genie Code can connect.
*
* Uses @modelcontextprotocol/sdk with StreamableHTTPServerTransport
* in stateless mode (fresh server per request).
*/
/**
* Extract the raw Zod shape from a ZodType so it can be passed as
* `inputSchema` to McpServer.registerTool().
*
* The MCP SDK expects a `ZodRawShapeCompat` — a `Record<string, ZodType>` —
* which is the shape argument of `z.object({...})`.
*
* Returns undefined for non-object schemas (primitive, array, etc.)
* so the tool is still registered but without parameter definitions.
*/
function extractZodShape(schema) {
	const v4Internal = schema;
	if (v4Internal._zod?.def?.shape) {
		const shape = v4Internal._zod.def.shape;
		return typeof shape === "function" ? shape() : shape;
	}
	const v3Internal = schema;
	if (v3Internal._def?.typeName === "ZodObject") {
		const shape = v3Internal._def?.shape ?? v3Internal.shape;
		if (shape) return typeof shape === "function" ? shape() : shape;
	}
	if (v3Internal.shape) {
		const shape = v3Internal.shape;
		return typeof shape === "function" ? shape() : shape;
	}
}
const mcpAuthStore = new AsyncLocalStorage();
function getMcpAuth() {
	return mcpAuthStore.getStore();
}
function createMcpPlugin(config, agentExports) {
	const mcpPath = config.path ?? "/mcp";
	/** Create a fresh MCP server with tools registered. */
	async function createServer() {
		const { McpServer } = await import("@modelcontextprotocol/sdk/server/mcp.js");
		const server = new McpServer({
			name: "appkit-agent",
			version: "1.0.0"
		}, { capabilities: { tools: {} } });
		const exports = agentExports();
		if (exports) for (const t of exports.getTools()) {
			const zodShape = extractZodShape(t.parameters);
			server.registerTool(t.name, {
				description: t.description,
				...zodShape ? { inputSchema: zodShape } : {}
			}, async (args) => {
				try {
					const mcpAuth = mcpAuthStore.getStore();
					const oboHeaders = {};
					if (mcpAuth?.oboToken) oboHeaders["x-forwarded-access-token"] = mcpAuth.oboToken;
					if (mcpAuth?.authorization) oboHeaders["authorization"] = mcpAuth.authorization;
					const result = await runWithContext({ oboHeaders }, () => t.handler(args));
					return { content: [{
						type: "text",
						text: typeof result === "string" ? result : JSON.stringify(result)
					}] };
				} catch (e) {
					return {
						content: [{
							type: "text",
							text: `Tool error: ${e instanceof Error ? e.message : String(e)}`
						}],
						isError: true
					};
				}
			});
		}
		return server;
	}
	return {
		name: "mcp",
		displayName: "MCP Server",
		description: "Model Context Protocol server for agent tool access",
		async setup() {
			try {
				await import("@modelcontextprotocol/sdk/server/mcp.js");
			} catch (e) {
				console.warn("MCP SDK not available:", e);
			}
		},
		injectRoutes(router) {
			router.all(mcpPath, async (req, res) => {
				const authCtx = {
					authorization: req.headers.authorization ?? "",
					oboToken: req.headers["x-forwarded-access-token"] ?? ""
				};
				try {
					const { StreamableHTTPServerTransport } = await import("@modelcontextprotocol/sdk/server/streamableHttp.js");
					const server = await createServer();
					const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: void 0 });
					await mcpAuthStore.run(authCtx, async () => {
						await server.connect(transport);
						await transport.handleRequest(req, res, req.body);
					});
				} catch (e) {
					if (!res.headersSent) res.status(500).json({ error: `MCP error: ${e instanceof Error ? e.message : String(e)}` });
				}
			});
		}
	};
}

//#endregion
//#region src/dev/index.ts
function createDevPlugin(config, agentExports) {
	const basePath = config.basePath ?? "/_apx";
	const guardProduction = config.productionGuard ?? true;
	return {
		name: "devUI",
		displayName: "Agent Dev UI",
		description: "Development chat UI and tool inspector",
		injectRoutes(router) {
			if (guardProduction && process.env.NODE_ENV === "production") return;
			router.get(`${basePath}/tools`, (_req, res) => {
				const exports = agentExports();
				if (!exports) {
					res.json({
						tools: [],
						message: "Agent plugin not available"
					});
					return;
				}
				const tools = exports.getTools().map((t) => ({
					name: t.name,
					description: t.description
				}));
				const schemas = exports.getToolSchemas();
				res.json({
					tools,
					schemas
				});
			});
			router.get(`${basePath}/agent`, (_req, res) => {
				res.type("html").send(chatPageHtml(basePath));
			});
			router.get(`${basePath}/probe`, async (req, res) => {
				const targetUrl = req.query.url;
				if (!targetUrl) {
					res.status(400).json({ error: "url query parameter required" });
					return;
				}
				let parsed;
				try {
					parsed = new URL(targetUrl);
				} catch {
					res.status(400).json({ error: "Invalid URL" });
					return;
				}
				if (!["http:", "https:"].includes(parsed.protocol)) {
					res.status(400).json({ error: "Only http/https URLs allowed" });
					return;
				}
				const host = parsed.hostname.toLowerCase();
				if (host === "localhost" || host.startsWith("127.") || host.startsWith("10.") || host.startsWith("192.168.") || host === "169.254.169.254" || host.startsWith("0.") || host === "[::1]" || /^172\.(1[6-9]|2\d|3[01])\./.test(host)) {
					res.status(403).json({ error: "Private/internal addresses not allowed" });
					return;
				}
				try {
					const start = Date.now();
					const response = await fetch(targetUrl);
					res.json({
						url: targetUrl,
						status: response.status,
						ok: response.ok,
						elapsed_ms: Date.now() - start
					});
				} catch (err) {
					res.json({
						url: targetUrl,
						error: err instanceof Error ? err.message : String(err)
					});
				}
			});
			router.get(`${basePath}/traces`, (_req, res) => {
				const traces = getTraces();
				res.setHeader("Content-Type", "text/html");
				res.send(tracesListHtml(traces, basePath));
			});
			router.get(`${basePath}/traces/:traceId`, (req, res) => {
				const trace = getTrace(req.params.traceId);
				if (!trace) {
					res.status(404).send("Trace not found");
					return;
				}
				res.setHeader("Content-Type", "text/html");
				res.send(traceDetailHtml(trace, basePath));
			});
		}
	};
}
function chatPageHtml(basePath) {
	return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Agent Dev UI</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }
    header { padding: 1rem; background: #16213e; border-bottom: 1px solid #333; }
    header h1 { font-size: 1.1rem; font-weight: 600; }
    #messages { flex: 1; overflow-y: auto; padding: 1rem; }
    .msg { margin-bottom: 0.75rem; padding: 0.75rem; border-radius: 8px; max-width: 80%; white-space: pre-wrap; }
    .msg.user { background: #0f3460; margin-left: auto; }
    .msg.assistant { background: #1a1a2e; border: 1px solid #333; }
    #input-bar { display: flex; gap: 0.5rem; padding: 1rem; background: #16213e; border-top: 1px solid #333; }
    #input-bar input { flex: 1; padding: 0.75rem; border-radius: 6px; border: 1px solid #444; background: #1a1a2e; color: #e0e0e0; font-size: 0.9rem; }
    #input-bar button { padding: 0.75rem 1.5rem; border-radius: 6px; border: none; background: #e94560; color: white; font-weight: 600; cursor: pointer; }
    nav { padding: 0.5rem 1rem; background: #16213e; font-size: 0.8rem; }
    nav a { color: #e94560; margin-right: 1rem; text-decoration: none; }
    nav a:hover { text-decoration: underline; }
    .streaming { opacity: 0.7; }
  </style>
</head>
<body>
  <header><h1>Agent Dev UI</h1></header>
  <nav>
    <a href="${basePath}/agent">Chat</a>
    <a href="${basePath}/tools">Tools</a>
    <a href="${basePath}/traces">Traces</a>
    <a href="/.well-known/agent.json" target="_blank">Agent Card</a>
  </nav>
  <div id="messages"></div>
  <div id="input-bar">
    <input id="input" type="text" placeholder="Ask the agent..." autofocus />
    <button onclick="send()">Send</button>
  </div>
  <script>
    const msgs = document.getElementById('messages');
    const input = document.getElementById('input');
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });

    async function send() {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      addMsg('user', text);

      // Try streaming first
      const assistantDiv = addMsg('assistant', '', true);
      try {
        const res = await fetch('/responses', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ input: [{ role: 'user', content: text }], stream: true }),
        });

        if (res.headers.get('content-type')?.includes('text/event-stream')) {
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let fullText = '';
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            const chunk = decoder.decode(value);
            for (const line of chunk.split('\\n')) {
              if (line.startsWith('data: ')) {
                try {
                  const data = JSON.parse(line.slice(6));
                  if (data.text) { fullText += data.text; assistantDiv.textContent = fullText; }
                  if (data.output) { assistantDiv.textContent = data.output?.content?.[0]?.text || fullText; }
                } catch {}
              }
            }
          }
          assistantDiv.classList.remove('streaming');
        } else {
          const data = await res.json();
          const reply = data?.output_text ?? data?.output?.[0]?.content?.[0]?.text ?? JSON.stringify(data);
          assistantDiv.textContent = reply;
          assistantDiv.classList.remove('streaming');
        }
      } catch (err) {
        assistantDiv.textContent = 'Error: ' + err.message;
        assistantDiv.classList.remove('streaming');
      }
      msgs.scrollTop = msgs.scrollHeight;
    }

    function addMsg(role, text, streaming = false) {
      const div = document.createElement('div');
      div.className = 'msg ' + role + (streaming ? ' streaming' : '');
      div.textContent = text || (streaming ? 'Thinking...' : '');
      msgs.appendChild(div);
      msgs.scrollTop = msgs.scrollHeight;
      return div;
    }
  <\/script>
</body>
</html>`;
}
function truncateStr(value, maxLen = 120) {
	const s = typeof value === "string" ? value : JSON.stringify(value);
	if (!s) return "";
	return s.length > maxLen ? s.slice(0, maxLen) + "..." : s;
}
function escapeHtml(str) {
	return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function statusBadge(status) {
	return `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:${{
		in_progress: "#f0ad4e",
		completed: "#5cb85c",
		error: "#d9534f"
	}[status ?? ""] ?? "#888"};color:#fff;font-size:0.75rem;font-weight:600;">${escapeHtml(status ?? "unknown")}</span>`;
}
function tracesListHtml(traces, basePath) {
	return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="10">
  <title>Agent Traces</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0; min-height: 100vh; }
    header { padding: 1rem; background: #16213e; border-bottom: 1px solid #333; }
    header h1 { font-size: 1.1rem; font-weight: 600; }
    nav { padding: 0.5rem 1rem; background: #16213e; font-size: 0.8rem; }
    nav a { color: #e94560; margin-right: 1rem; text-decoration: none; }
    nav a:hover { text-decoration: underline; }
    .summary { padding: 1rem; display: flex; gap: 1.5rem; font-size: 0.85rem; color: #aaa; }
    .summary span { font-weight: 600; color: #e0e0e0; }
    table { width: 100%; border-collapse: collapse; }
    th { text-align: left; padding: 8px 12px; border-bottom: 2px solid #444; font-size: 0.8rem; color: #aaa; text-transform: uppercase; letter-spacing: 0.05em; }
    tr:hover { background: #16213e; }
  </style>
</head>
<body>
  <header><h1>Agent Traces</h1></header>
  <nav>
    <a href="${basePath}/agent">Chat</a>
    <a href="${basePath}/tools">Tools</a>
    <a href="${basePath}/traces">Traces</a>
    <a href="/.well-known/agent.json" target="_blank">Agent Card</a>
  </nav>
  <div class="summary">
    <div>Total: <span>${traces.length}</span></div>
    <div>In Progress: <span>${traces.filter((t) => t.status === "in_progress").length}</span></div>
    <div>Completed: <span>${traces.filter((t) => t.status === "completed").length}</span></div>
    <div>Errors: <span>${traces.filter((t) => t.status === "error").length}</span></div>
  </div>
  <table>
    <thead>
      <tr>
        <th>Trace ID</th>
        <th>Agent</th>
        <th>Status</th>
        <th>Spans</th>
        <th>Duration</th>
        <th>Input</th>
      </tr>
    </thead>
    <tbody>
      ${traces.map((t) => {
		const firstInput = t.spans.find((s) => s.type === "request");
		const inputPreview = firstInput ? truncateStr(firstInput.input, 80) : "";
		const duration = t.duration_ms != null ? `${t.duration_ms}ms` : "running";
		return `<tr onclick="location.href='${basePath}/traces/${t.id}'" style="cursor:pointer;">
        <td style="padding:8px 12px;border-bottom:1px solid #333;font-family:monospace;font-size:0.8rem;">${escapeHtml(t.id)}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;">${escapeHtml(t.agentName)}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;">${statusBadge(t.status)}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;text-align:center;">${t.spans.length}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;text-align:right;font-family:monospace;">${duration}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;font-size:0.85rem;color:#aaa;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(inputPreview)}</td>
      </tr>`;
	}).join("\n") || "<tr><td colspan=\"6\" style=\"padding:2rem;text-align:center;color:#666;\">No traces yet</td></tr>"}
    </tbody>
  </table>
</body>
</html>`;
}
/**
* Extract a readable message from a span's input/output.
* Tries to pull out the human-meaningful content instead of showing raw structures.
*/
function extractMessage(value) {
	if (value == null) return "";
	if (typeof value === "string") try {
		return extractMessage(JSON.parse(value));
	} catch {
		return value.slice(0, 500);
	}
	if (typeof value === "number") return String(value);
	if (Array.isArray(value)) {
		for (let i = value.length - 1; i >= 0; i--) {
			const msg = value[i];
			if (msg && typeof msg === "object" && "content" in msg) return extractMessage(msg.content);
		}
		return value.map((v) => extractMessage(v)).filter(Boolean).join(", ").slice(0, 300);
	}
	if (typeof value === "object") {
		const obj = value;
		if ("content" in obj) return extractMessage(obj.content);
		if ("text" in obj) return extractMessage(obj.text);
		if ("output_text" in obj) return extractMessage(obj.output_text);
		if ("message" in obj) return extractMessage(obj.message);
		return Object.entries(obj).filter(([, v]) => v != null && v !== "").map(([k, v]) => {
			if (typeof v === "number") return `${k}: ${v}`;
			if (typeof v === "string") return v.length > 60 ? `${k}: ${v.slice(0, 60)}...` : `${k}: ${v}`;
			if (Array.isArray(v)) return `${k}: [${v.length} items]`;
			return `${k}: ${JSON.stringify(v).slice(0, 40)}`;
		}).join("\n");
	}
	return String(value).slice(0, 300);
}
function spanBubble(span) {
	const duration = span.duration_ms != null ? `${(span.duration_ms / 1e3).toFixed(1)}s` : "";
	if (span.type === "request") return `<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#7986cb;"></div>
      <div class="step-content">
        <div class="step-header"><span class="who" style="color:#7986cb;">Caller</span></div>
        <div class="bubble caller">${escapeHtml(extractMessage(span.input) || "Request received")}</div>
      </div>
    </div>`;
	if (span.type === "llm") {
		const model = span.metadata?.model ? String(span.metadata.model).replace("databricks-", "") : "LLM";
		const input = extractMessage(span.input);
		const output = extractMessage(span.output);
		return `<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#00bcd4;"></div>
      <div class="step-content">
        <div class="step-header">
          <span class="who" style="color:#00bcd4;">Agent asked ${escapeHtml(model)}</span>
          ${duration ? `<span class="dur">${duration}</span>` : ""}
        </div>
        ${input ? `<div class="bubble agent-ask">${escapeHtml(input)}</div>` : ""}
        ${output ? `<div class="bubble llm-reply">${escapeHtml(output)}</div>` : ""}
      </div>
    </div>`;
	}
	if (span.type === "tool") {
		const input = extractMessage(span.input);
		const output = extractMessage(span.output);
		return `<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#ffb300;"></div>
      <div class="step-content">
        <div class="step-header">
          <span class="who" style="color:#ffb300;">Called tool <em>${escapeHtml(span.name)}</em></span>
          ${duration ? `<span class="dur">${duration}</span>` : ""}
        </div>
        ${input ? `<div class="bubble tool-in">${input.split("\n").map((l) => `<div class="kv">${escapeHtml(l)}</div>`).join("")}</div>` : ""}
        ${output ? `<div class="bubble tool-out">${output.split("\n").map((l) => {
			const match = l.match(/^(\w+):\s*([0-9.]+)$/);
			if (match) {
				const v = parseFloat(match[2]);
				const color = v >= .5 ? "#4caf50" : v > 0 ? "#ffb74d" : "#888";
				return `<div class="kv"><span class="kv-key">${escapeHtml(match[1])}</span><span style="color:${color};font-weight:600;">${match[2]}</span></div>`;
			}
			return `<div class="kv">${escapeHtml(l)}</div>`;
		}).join("")}</div>` : ""}
      </div>
    </div>`;
	}
	if (span.type === "agent_call") {
		const output = extractMessage(span.output);
		return `<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#ab47bc;"></div>
      <div class="step-content">
        <div class="step-header">
          <span class="who" style="color:#ab47bc;">Called agent <em>${escapeHtml(span.name)}</em></span>
          ${duration ? `<span class="dur">${duration}</span>` : ""}
        </div>
        ${output ? `<div class="bubble agent-reply">${escapeHtml(output)}</div>` : ""}
      </div>
    </div>`;
	}
	if (span.type === "response") return `<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#4caf50;"></div>
      <div class="step-content">
        <div class="step-header"><span class="who" style="color:#4caf50;">Agent responded</span></div>
        <div class="bubble response">${escapeHtml(extractMessage(span.output) || "Done")}</div>
      </div>
    </div>`;
	if (span.type === "error") return `<div class="step">
      <div class="step-line"></div>
      <div class="step-dot" style="background:#f44336;"></div>
      <div class="step-content">
        <div class="step-header"><span class="who" style="color:#f44336;">Error</span></div>
        <div class="bubble error-msg">${escapeHtml((span.metadata?.error ? String(span.metadata.error) : extractMessage(span.output)) || "Unknown error")}</div>
      </div>
    </div>`;
	return "";
}
function traceDetailHtml(trace, basePath) {
	const duration = trace.duration_ms != null ? `${(trace.duration_ms / 1e3).toFixed(1)}s` : "in progress";
	const spans = trace.spans.map(spanBubble).join("\n");
	const statusColor = trace.status === "completed" ? "#4caf50" : trace.status === "error" ? "#f44336" : "#ffb74d";
	return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trace: ${escapeHtml(trace.agentName)}</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a14; color: #e0e0e0; min-height: 100vh; }

    .top-bar { padding: 12px 20px; background: #12121e; border-bottom: 1px solid #1e1e30; display: flex; align-items: center; gap: 12px; }
    .top-bar a { color: #7986cb; text-decoration: none; font-size: 13px; }
    .top-bar h1 { font-size: 16px; font-weight: 600; flex: 1; }
    .top-bar .status { padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .top-bar .meta { font-size: 12px; color: #666; }

    .conversation { max-width: 700px; margin: 0 auto; padding: 24px 20px; }

    .step { position: relative; padding-left: 28px; margin-bottom: 4px; }
    .step-line { position: absolute; left: 8px; top: 20px; bottom: -4px; width: 1px; background: #1e1e30; }
    .step:last-child .step-line { display: none; }
    .step-dot { position: absolute; left: 3px; top: 6px; width: 11px; height: 11px; border-radius: 50%; }
    .step-content { padding-bottom: 12px; }
    .step-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
    .who { font-size: 13px; font-weight: 600; }
    .dur { font-size: 11px; color: #555; }

    .bubble { padding: 10px 14px; border-radius: 10px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; max-width: 600px; }

    .bubble.caller { background: #1a1a30; color: #b0b0c8; border: 1px solid #252545; }
    .bubble.agent-ask { background: #0a1a25; color: #80cbc4; border: 1px solid #1a3040; font-size: 13px; }
    .bubble.llm-reply { background: #12222e; color: #e0f0f0; border: 1px solid #1a3545; margin-top: 6px; }
    .bubble.tool-in { background: #1a1800; color: #d4c87a; border: 1px solid #2a2500; font-size: 13px; }
    .bubble.tool-out { background: #1a1a08; color: #e0d8a0; border: 1px solid #2a2810; margin-top: 6px; }
    .bubble.agent-reply { background: #1a0a25; color: #d1a0e8; border: 1px solid #2a1a40; }
    .bubble.response { background: #0a1a0a; color: #a0d8a0; border: 1px solid #1a3020; }
    .bubble.error-msg { background: #1a0a0a; color: #f08080; border: 1px solid #3a1a1a; }

    .kv { padding: 2px 0; }
    .kv-key { color: #888; margin-right: 8px; }
    .kv-key::after { content: ':'; }
  </style>
</head>
<body>
  <div class="top-bar">
    <a href="${basePath}/traces">&larr; All traces</a>
    <h1>${escapeHtml(trace.agentName)}</h1>
    <span class="status" style="background:${statusColor}20;color:${statusColor};">${trace.status || "unknown"}</span>
    <span class="meta">${duration} &middot; ${trace.spans.length} steps</span>
  </div>
  <nav>
    <a href="${basePath}/traces">&larr; Back to Traces</a>
    <a href="${basePath}/agent">Chat</a>
  <div class="conversation">
    ${spans || "<div style=\"padding:3rem;text-align:center;color:#555;\">No steps recorded</div>"}
  </div>
</body>
</html>`;
}

//#endregion
//#region src/workflows/engine.ts
/**
* Thrown when a handler raised an error that the engine persisted. Replay of
* a previously failed step re-throws this so callers see the same failure
* they would have seen originally.
*/
var StepFailedError = class extends Error {
	stepKey;
	constructor(stepKey, message) {
		super(message);
		this.name = "StepFailedError";
		this.stepKey = stepKey;
	}
};

//#endregion
//#region src/workflows/engine-memory.ts
/**
* InMemoryEngine — default WorkflowEngine backend.
*
* Stores runs and step records in a process-local Map. Preserves the
* workflow API's step-caching and replay semantics so tests can exercise
* resumption without a SQL warehouse, but loses all state on process exit.
* Use `DeltaEngine` (Phase 4) for real durability.
*/
var InMemoryEngine = class {
	runs = /* @__PURE__ */ new Map();
	async startRun(workflowName, input, opts) {
		const now = (/* @__PURE__ */ new Date()).toISOString();
		const existing = opts?.runId ? this.runs.get(opts.runId) : void 0;
		if (existing) {
			existing.status = "running";
			existing.updatedAt = now;
			return existing.runId;
		}
		const runId = opts?.runId ?? randomUUID();
		this.runs.set(runId, {
			runId,
			workflowName,
			status: "running",
			input: structuredClone(input),
			startedAt: now,
			updatedAt: now,
			steps: /* @__PURE__ */ new Map()
		});
		return runId;
	}
	async step(runId, stepKey, handler) {
		const run = this.runs.get(runId);
		if (!run) throw new Error(`Unknown runId: ${runId}`);
		const cached = run.steps.get(stepKey);
		if (cached) {
			if (cached.status === "completed") return structuredClone(cached.output);
			throw new StepFailedError(stepKey, cached.error ?? "step failed");
		}
		const start = Date.now();
		try {
			const result = await handler();
			const record = {
				stepKey,
				status: "completed",
				output: structuredClone(result),
				durationMs: Date.now() - start,
				recordedAt: (/* @__PURE__ */ new Date()).toISOString()
			};
			run.steps.set(stepKey, record);
			run.updatedAt = record.recordedAt;
			return result;
		} catch (err) {
			const record = {
				stepKey,
				status: "failed",
				error: err instanceof Error ? err.message : String(err),
				durationMs: Date.now() - start,
				recordedAt: (/* @__PURE__ */ new Date()).toISOString()
			};
			run.steps.set(stepKey, record);
			run.updatedAt = record.recordedAt;
			throw err;
		}
	}
	async finishRun(runId, status, output) {
		const run = this.runs.get(runId);
		if (!run) throw new Error(`Unknown runId: ${runId}`);
		run.status = status;
		if (output !== void 0) run.output = structuredClone(output);
		run.updatedAt = (/* @__PURE__ */ new Date()).toISOString();
	}
	async getRun(runId) {
		const run = this.runs.get(runId);
		if (!run) return null;
		return {
			runId: run.runId,
			workflowName: run.workflowName,
			status: run.status,
			input: structuredClone(run.input),
			output: run.output === void 0 ? void 0 : structuredClone(run.output),
			startedAt: run.startedAt,
			updatedAt: run.updatedAt,
			steps: Array.from(run.steps.values()).map((s) => ({
				...s,
				output: structuredClone(s.output)
			}))
		};
	}
	async listRuns(filter) {
		let results = Array.from(this.runs.values());
		if (filter?.workflowName) results = results.filter((r) => r.workflowName === filter.workflowName);
		if (filter?.status) results = results.filter((r) => r.status === filter.status);
		results.sort((a, b) => b.startedAt.localeCompare(a.startedAt));
		if (filter?.limit !== void 0) results = results.slice(0, filter.limit);
		return results.map((r) => ({
			runId: r.runId,
			workflowName: r.workflowName,
			status: r.status,
			startedAt: r.startedAt,
			updatedAt: r.updatedAt
		}));
	}
};

//#endregion
//#region src/workflows/state.ts
/**
* AgentState — shared key-value store for workflow agents.
*
* Follows the Google ADK pattern:
* - `output_key`: when an agent has an outputKey, its result is stored under that key
* - Template interpolation: `{variable_name}` in instruction strings resolves from state
* - Scoped state: keys prefixed with `temp:` are turn-specific and cleared between steps
*
* @example
* const state = new AgentState({ topic: 'billing' });
* state.set('analysis', 'The billing data shows...');
*
* // Template interpolation
* const instructions = state.interpolate('Summarize the {topic} analysis: {analysis}');
* // => 'Summarize the billing analysis: The billing data shows...'
*
* // Scoped temp values
* state.set('temp:scratchpad', 'intermediate work');
* state.clearTemp();
* state.has('temp:scratchpad'); // false
*/
/** Prefix for turn-scoped keys that get cleared between steps. */
const TEMP_PREFIX = "temp:";
var AgentState = class AgentState {
	store;
	constructor(initial) {
		this.store = new Map(initial ? Object.entries(initial) : []);
	}
	/** Get a value by key. Returns undefined if not present. */
	get(key) {
		return this.store.get(key);
	}
	/** Set a value. Use `temp:` prefix for turn-scoped data. */
	set(key, value) {
		this.store.set(key, value);
	}
	/** Check if a key exists. */
	has(key) {
		return this.store.has(key);
	}
	/** Delete a key. */
	delete(key) {
		return this.store.delete(key);
	}
	/** Return all keys. */
	keys() {
		return Array.from(this.store.keys());
	}
	/** Return all entries as a plain object. */
	toObject() {
		return Object.fromEntries(this.store);
	}
	/**
	* Clear all keys with the `temp:` prefix.
	* Called between agent steps in a sequential pipeline so
	* temporary scratchpad data doesn't leak across turns.
	*/
	clearTemp() {
		for (const key of this.store.keys()) if (key.startsWith(TEMP_PREFIX)) this.store.delete(key);
	}
	/**
	* Replace `{variable_name}` placeholders in a string with state values.
	*
	* Only replaces variables that exist in state. Unknown placeholders are
	* left as-is so downstream agents can still reference them (or the caller
	* gets a clear signal that the variable wasn't set).
	*
	* Values are coerced to strings via `String()`.
	*/
	interpolate(template) {
		return template.replace(/\{(\w+(?::\w+)?)\}/g, (match, key) => {
			if (this.store.has(key)) return String(this.store.get(key));
			return match;
		});
	}
	/** Create a shallow copy of this state. */
	clone() {
		const copy = new AgentState();
		for (const [k, v] of this.store) copy.set(k, v);
		return copy;
	}
};

//#endregion
//#region src/workflows/sequential.ts
var SequentialAgent = class {
	agents;
	instructions;
	engine;
	providedRunId;
	workflowName;
	constructor(agents, instructions = "", options = {}) {
		if (agents.length === 0) throw new Error("SequentialAgent requires at least one agent");
		this.agents = agents;
		this.instructions = instructions;
		this.engine = options.engine ?? new InMemoryEngine();
		this.providedRunId = options.runId;
		this.workflowName = options.workflowName ?? "sequential";
	}
	async run(messages, state) {
		const agentState = state ?? new AgentState();
		const runId = await this.engine.startRun(this.workflowName, {
			messages,
			instructions: this.instructions
		}, { runId: this.providedRunId });
		let context = this.prependInstructions(messages, agentState);
		let result = "";
		for (let i = 0; i < this.agents.length; i++) {
			const agent = this.agents[i];
			agentState.clearTemp();
			result = await this.engine.step(runId, `step-${i}`, () => agent.run(context, agentState));
			if (agent.outputKey) agentState.set(agent.outputKey, result);
			context = [...context, {
				role: "assistant",
				content: result
			}];
		}
		await this.engine.finishRun(runId, "completed", result);
		return result;
	}
	async *stream(messages, state) {
		const agentState = state ?? new AgentState();
		let context = this.prependInstructions(messages, agentState);
		for (const agent of this.agents.slice(0, -1)) {
			agentState.clearTemp();
			const result = await agent.run(context, agentState);
			if (agent.outputKey) agentState.set(agent.outputKey, result);
			context = [...context, {
				role: "assistant",
				content: result
			}];
		}
		const last = this.agents[this.agents.length - 1];
		agentState.clearTemp();
		let lastResult;
		if (last.stream) {
			const chunks = [];
			for await (const chunk of last.stream(context, agentState)) {
				chunks.push(chunk);
				yield chunk;
			}
			lastResult = chunks.join("");
		} else {
			lastResult = await last.run(context, agentState);
			yield lastResult;
		}
		if (last.outputKey) agentState.set(last.outputKey, lastResult);
	}
	collectTools() {
		return this.agents.flatMap((a) => a.collectTools?.() ?? []);
	}
	/**
	* Prepend system instructions to messages.
	* If state is provided, interpolate {variables} in the instructions.
	*/
	prependInstructions(messages, agentState) {
		if (!this.instructions) return [...messages];
		return [{
			role: "system",
			content: agentState ? agentState.interpolate(this.instructions) : this.instructions
		}, ...messages];
	}
};

//#endregion
//#region src/workflows/parallel.ts
var ParallelAgent = class {
	agents;
	instructions;
	separator;
	constructor(agents, options = {}) {
		if (agents.length === 0) throw new Error("ParallelAgent requires at least one agent");
		this.agents = agents;
		this.instructions = options.instructions ?? "";
		this.separator = options.separator ?? "\n\n";
	}
	async run(messages) {
		const context = this.prependInstructions(messages);
		return (await Promise.all(this.agents.map((agent) => agent.run(context)))).join(this.separator);
	}
	async *stream(messages) {
		yield await this.run(messages);
	}
	collectTools() {
		return this.agents.flatMap((a) => a.collectTools?.() ?? []);
	}
	prependInstructions(messages) {
		if (!this.instructions) return [...messages];
		return [{
			role: "system",
			content: this.instructions
		}, ...messages];
	}
};

//#endregion
//#region src/workflows/loop.ts
var LoopAgent = class {
	agent;
	maxIterations;
	stopWhen;
	engine;
	providedRunId;
	workflowName;
	constructor(agent, options = {}) {
		this.agent = agent;
		this.maxIterations = options.maxIterations ?? 5;
		this.stopWhen = options.stopWhen ?? null;
		this.engine = options.engine ?? new InMemoryEngine();
		this.providedRunId = options.runId;
		this.workflowName = options.workflowName ?? "loop";
	}
	async run(messages) {
		const runId = await this.engine.startRun(this.workflowName, {
			messages,
			maxIterations: this.maxIterations
		}, { runId: this.providedRunId });
		const completed = ((await this.engine.getRun(runId))?.steps ?? []).filter((s) => s.stepKey.startsWith("iter-") && s.status === "completed").map((s) => ({
			iter: Number.parseInt(s.stepKey.slice(5), 10),
			result: s.output
		})).sort((a, b) => a.iter - b.iter);
		let context = [...messages];
		let result = "";
		let nextIter = 0;
		for (const { iter, result: iterResult } of completed) {
			result = iterResult;
			if (this.stopWhen?.(result, iter)) {
				await this.engine.finishRun(runId, "completed", result);
				return result;
			}
			context = [...context, {
				role: "assistant",
				content: result
			}];
			nextIter = iter + 1;
		}
		for (let i = nextIter; i < this.maxIterations; i++) {
			result = await this.engine.step(runId, `iter-${i}`, () => this.agent.run(context));
			if (this.stopWhen?.(result, i)) break;
			context = [...context, {
				role: "assistant",
				content: result
			}];
		}
		await this.engine.finishRun(runId, "completed", result);
		return result;
	}
	async *stream(messages) {
		yield await this.run(messages);
	}
	collectTools() {
		return this.agent.collectTools?.() ?? [];
	}
};

//#endregion
//#region src/workflows/router.ts
var RouterAgent = class {
	routes;
	instructions;
	fallback;
	constructor(config) {
		if (config.routes.length === 0) throw new Error("RouterAgent requires at least one route");
		this.routes = config.routes;
		this.instructions = config.instructions ?? "";
		this.fallback = config.fallback ?? null;
	}
	async run(messages) {
		return this.selectRoute(messages).run(messages);
	}
	async *stream(messages) {
		const target = this.selectRoute(messages);
		if (target.stream) yield* target.stream(messages);
		else yield await target.run(messages);
	}
	collectTools() {
		return this.routes.flatMap((r) => r.agent.collectTools?.() ?? []);
	}
	selectRoute(messages) {
		for (const route of this.routes) if (route.condition?.(messages)) return route.agent;
		return this.fallback ?? this.routes[0].agent;
	}
};

//#endregion
//#region src/workflows/handoff.ts
/**
* Wraps a Runnable to detect handoff requests in its output.
*
* The wrapped agent's response is checked for `transfer_to_<name>` patterns.
* This is a simple text-matching approach — for real production use, the
* underlying agent should use function calling with transfer tools.
*/
var HandoffAgent = class {
	agents;
	start;
	maxHandoffs;
	onHandoff;
	constructor(config) {
		if (!(config.start in config.agents)) throw new Error(`HandoffAgent start='${config.start}' not found in agents`);
		this.agents = config.agents;
		this.start = config.start;
		this.maxHandoffs = config.maxHandoffs ?? 5;
		this.onHandoff = config.onHandoff;
	}
	async run(messages) {
		let currentName = this.start;
		let context = [...messages];
		let result = "";
		for (let i = 0; i <= this.maxHandoffs; i++) {
			const agent = this.agents[currentName];
			if (!agent) return `Error: agent '${currentName}' not found`;
			const transferNames = Object.keys(this.agents).filter((n) => n !== currentName);
			const transferInstructions = transferNames.length ? `\nYou can hand off to: ${transferNames.map((n) => `transfer_to_${n}`).join(", ")}. To hand off, respond with exactly "TRANSFER: <agent_name>" on its own line.` : "";
			const agentMessages = [...context, ...transferInstructions ? [{
				role: "system",
				content: transferInstructions
			}] : []];
			result = await agent.run(agentMessages);
			const handoffMatch = result.match(/TRANSFER:\s*(\w+)/i);
			if (handoffMatch) {
				const targetName = handoffMatch[1];
				if (targetName in this.agents && targetName !== currentName) {
					this.onHandoff?.(currentName, targetName, result);
					context = [
						...context,
						{
							role: "assistant",
							content: result
						},
						{
							role: "system",
							content: `[Handed off from ${currentName} to ${targetName}]`
						}
					];
					currentName = targetName;
					continue;
				}
			}
			break;
		}
		return result;
	}
	async *stream(messages) {
		yield await this.run(messages);
	}
	collectTools() {
		return Object.values(this.agents).flatMap((a) => a.collectTools?.() ?? []);
	}
};

//#endregion
//#region src/workflows/remote.ts
var RemoteAgent = class RemoteAgent {
	/** Agent card metadata — populated after `init()`. */
	card = null;
	cardUrl;
	baseUrl;
	headers;
	timeoutMs;
	initPromise = null;
	constructor(config) {
		this.cardUrl = config.cardUrl;
		this.baseUrl = config.cardUrl.replace(/\/?\.well-known\/agent\.json$/, "").replace(/\/$/, "");
		this.headers = config.headers ?? {};
		this.timeoutMs = config.timeoutMs ?? 12e4;
	}
	/**
	* Create a RemoteAgent from a full agent card URL.
	* The card is fetched eagerly so metadata is available immediately.
	*/
	static async fromCardUrl(cardUrl, headers) {
		const agent = new RemoteAgent({
			cardUrl,
			headers
		});
		await agent.init();
		return agent;
	}
	/**
	* Create a RemoteAgent from a Databricks App name.
	*
	* Constructs the agent card URL from `DATABRICKS_HOST`:
	*   `https://<host>/apps/<appName>/.well-known/agent.json`
	*
	* Falls back to the apps subdomain pattern if DATABRICKS_HOST is not set
	* but DATABRICKS_WORKSPACE_ID is available.
	*/
	static async fromAppName(appName, headers) {
		const host = process.env.DATABRICKS_HOST?.replace(/\/$/, "");
		if (!host) throw new Error("RemoteAgent.fromAppName requires DATABRICKS_HOST environment variable. Use RemoteAgent.fromCardUrl() with a full URL instead.");
		const cardUrl = `${host}/apps/${appName}/.well-known/agent.json`;
		return RemoteAgent.fromCardUrl(cardUrl, headers);
	}
	/** Fetch the agent card. Safe to call multiple times (idempotent). */
	async init() {
		if (this.card) return;
		if (this.initPromise) return this.initPromise;
		this.initPromise = this.fetchCard();
		return this.initPromise;
	}
	async fetchCard() {
		const controller = new AbortController();
		const timeout = setTimeout(() => controller.abort(), 1e4);
		try {
			const res = await fetch(this.cardUrl, {
				headers: this.headers,
				signal: controller.signal
			});
			if (!res.ok) throw new Error(`Failed to fetch agent card from ${this.cardUrl}: ${res.status} ${res.statusText}`);
			this.card = await res.json();
			if (this.card.url) this.baseUrl = this.card.url.replace(/\/$/, "");
		} finally {
			clearTimeout(timeout);
		}
	}
	async run(messages) {
		await this.init();
		const payload = { input: messages.map((m) => ({
			role: m.role,
			content: m.content
		})) };
		const controller = new AbortController();
		const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
		try {
			const res = await fetch(`${this.baseUrl}/responses`, {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					...this.headers
				},
				body: JSON.stringify(payload),
				signal: controller.signal
			});
			if (!res.ok) throw new Error(`Remote agent ${this.card?.name ?? this.baseUrl} returned ${res.status}: ${await res.text()}`);
			const data = await res.json();
			return this.extractText(data);
		} finally {
			clearTimeout(timeout);
		}
	}
	async *stream(messages) {
		await this.init();
		const payload = {
			input: messages.map((m) => ({
				role: m.role,
				content: m.content
			})),
			stream: true
		};
		const controller = new AbortController();
		const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
		try {
			const res = await fetch(`${this.baseUrl}/responses`, {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					Accept: "text/event-stream",
					...this.headers
				},
				body: JSON.stringify(payload),
				signal: controller.signal
			});
			if (!res.ok) throw new Error(`Remote agent ${this.card?.name ?? this.baseUrl} stream returned ${res.status}: ${await res.text()}`);
			if (res.headers.get("content-type")?.includes("text/event-stream") && res.body) yield* this.parseSSE(res.body);
			else {
				const data = await res.json();
				yield this.extractText(data);
			}
		} finally {
			clearTimeout(timeout);
		}
	}
	collectTools() {
		return [];
	}
	get name() {
		return this.card?.name ?? "remote-agent";
	}
	get description() {
		return this.card?.description ?? "";
	}
	get skills() {
		return this.card?.skills ?? [];
	}
	extractText(data) {
		try {
			return data.output[0].content[0].text;
		} catch {
			return JSON.stringify(data);
		}
	}
	async *parseSSE(body) {
		const reader = body.getReader();
		const decoder = new TextDecoder();
		let buffer = "";
		try {
			while (true) {
				const { done, value } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true });
				const lines = buffer.split("\n");
				buffer = lines.pop() ?? "";
				for (const line of lines) if (line.startsWith("data: ")) {
					const payload = line.slice(6).trim();
					if (payload === "[DONE]") return;
					try {
						const event = JSON.parse(payload);
						if (typeof event.delta === "string") yield event.delta;
						else if (typeof event.text === "string") yield event.text;
					} catch {
						if (payload) yield payload;
					}
				}
			}
		} finally {
			reader.releaseLock();
		}
	}
};

//#endregion
//#region src/workflows/session.ts
/**
* Session — conversation session with message history, state, and pluggable persistence.
*
* A session ties together a conversation's messages and the shared AgentState
* so workflow agents can accumulate context across turns.
*
* @example
* const session = new Session();
* session.addMessage('user', 'Analyze the billing table');
* session.state.set('table', 'billing');
*
* // Save and restore
* await session.save();
* const restored = await Session.load(session.id);
*/
/** Simple in-memory store for development. Data is lost on process restart. */
var InMemorySessionStore = class {
	sessions = /* @__PURE__ */ new Map();
	async save(id, data) {
		this.sessions.set(id, structuredClone(data));
	}
	async load(id) {
		const snap = this.sessions.get(id);
		return snap ? structuredClone(snap) : null;
	}
	async delete(id) {
		this.sessions.delete(id);
	}
	async list() {
		return Array.from(this.sessions.keys());
	}
};
let defaultStore = new InMemorySessionStore();
/** Replace the default session store (e.g., with a Lakebase adapter). */
function setDefaultSessionStore(store) {
	defaultStore = store;
}
/** Get the current default session store. */
function getDefaultSessionStore() {
	return defaultStore;
}
var Session = class Session {
	id;
	state;
	messages;
	store;
	createdAt;
	updatedAt;
	constructor(options) {
		this.id = options?.id ?? randomUUID();
		this.state = options?.state ?? new AgentState();
		this.store = options?.store ?? defaultStore;
		this.messages = [];
		this.createdAt = (/* @__PURE__ */ new Date()).toISOString();
		this.updatedAt = this.createdAt;
	}
	/** Append a message to the conversation history. */
	addMessage(role, content) {
		this.messages.push({
			role,
			content
		});
		this.updatedAt = (/* @__PURE__ */ new Date()).toISOString();
	}
	/** Return a copy of the conversation history. */
	getHistory() {
		return [...this.messages];
	}
	/** Persist this session to the store. */
	async save() {
		const snapshot = {
			id: this.id,
			messages: [...this.messages],
			state: this.state.toObject(),
			createdAt: this.createdAt,
			updatedAt: (/* @__PURE__ */ new Date()).toISOString()
		};
		await this.store.save(this.id, snapshot);
	}
	/**
	* Load a session from the store.
	*
	* Returns a fully hydrated Session instance, or null if not found.
	*/
	static async load(id, store) {
		const s = store ?? defaultStore;
		const snapshot = await s.load(id);
		if (!snapshot) return null;
		const state = new AgentState(snapshot.state);
		const session = new Session({
			id: snapshot.id,
			state,
			store: s
		});
		for (const msg of snapshot.messages) session.messages.push(msg);
		session.createdAt = snapshot.createdAt;
		session.updatedAt = snapshot.updatedAt;
		return session;
	}
	/** Delete this session from the store. */
	async delete() {
		await this.store.delete(this.id);
	}
};

//#endregion
//#region src/workflows/hypothesis.ts
function createHypothesis(opts) {
	return {
		id: randomUUID().replace(/-/g, "").slice(0, 8),
		generation: opts.generation,
		parent_id: opts.parent_id ?? null,
		fitness: opts.fitness ?? {},
		metadata: opts.metadata ?? {},
		flagged_for_review: false,
		created_at: (/* @__PURE__ */ new Date()).toISOString()
	};
}
function compositeFitness(h, weights) {
	const entries = Object.entries(weights);
	if (entries.length === 0) return 0;
	let sum = 0;
	for (const [key, weight] of entries) sum += (h.fitness[key] ?? 0) * weight;
	return sum;
}

//#endregion
//#region src/workflows/pareto.ts
/**
* Returns true when `a` Pareto-dominates `b` with respect to `objectives`.
*
* Dominance requires:
*   1. a.fitness[obj] >= b.fitness[obj]  for ALL objectives
*   2. a.fitness[obj] >  b.fitness[obj]  for AT LEAST ONE objective
*
* Missing fitness values are treated as 0.
*/
function paretoDominates(a, b, objectives) {
	let strictlyBetterOnAtLeastOne = false;
	for (const obj of objectives) {
		const aScore = a.fitness[obj] ?? 0;
		const bScore = b.fitness[obj] ?? 0;
		if (aScore < bScore) return false;
		if (aScore > bScore) strictlyBetterOnAtLeastOne = true;
	}
	return strictlyBetterOnAtLeastOne;
}
/**
* Returns the Pareto-optimal (non-dominated) subset of `population`.
*
* A hypothesis is non-dominated when no other hypothesis in the population
* dominates it.  The algorithm is O(n²) — suitable for small populations.
*
* Returns an empty array for an empty population.
*/
function paretoFrontier(population, objectives) {
	if (population.length === 0) return [];
	return population.filter((candidate) => !population.some((other) => other !== candidate && paretoDominates(other, candidate, objectives)));
}
/**
* Selects at most `maxSize` survivors from `population`.
*
* Strategy:
*  1. Compute the Pareto frontier.
*  2. If the frontier already has >= maxSize members, return the top maxSize
*     ranked by composite fitness (descending).
*  3. Otherwise start with all frontier members, then fill remaining slots
*     from the non-frontier population ordered by composite fitness (descending).
*  4. If the whole population fits within maxSize, return all members
*     (ordered by composite fitness).
*/
function selectSurvivors(population, objectives, weights, maxSize) {
	if (population.length <= maxSize) return [...population].sort((a, b) => compositeFitness(b, weights) - compositeFitness(a, weights));
	const frontier = paretoFrontier(population, objectives);
	const frontierIds = new Set(frontier.map((h) => h.id));
	const nonFrontier = population.filter((h) => !frontierIds.has(h.id));
	const byFitnessDesc = (a, b) => compositeFitness(b, weights) - compositeFitness(a, weights);
	const sortedFrontier = [...frontier].sort(byFitnessDesc);
	if (sortedFrontier.length >= maxSize) return sortedFrontier.slice(0, maxSize);
	const sortedNonFrontier = [...nonFrontier].sort(byFitnessDesc);
	const remaining = maxSize - sortedFrontier.length;
	return [...sortedFrontier, ...sortedNonFrontier.slice(0, remaining)];
}

//#endregion
//#region src/workflows/evolutionary.ts
/**
* EvolutionaryAgent — runs a background evolutionary loop over a PopulationStore.
*
* Each generation:
*  1. Load top survivors from the previous generation
*  2. Mutate them via a mutation agent (POST to /responses)
*  3. Evaluate fitness via one or more fitness agents
*  4. Optionally judge the top cohort via a judge agent
*  5. Write new hypotheses + updated fitness back to the store
*  6. Select survivors via Pareto + composite fitness ranking
*  7. Escalate any hypothesis crossing the escalation threshold
*  8. Check convergence — stop if improvement is below threshold for N generations
*
* The agent also exposes 6 conversational tools so a user-facing chat UI can
* query state, pause/resume, and force-escalate without stopping the loop.
*/
var EvolutionaryAgent = class {
	_state = "idle";
	_currentGeneration = 0;
	_history = [];
	_loopPromise = null;
	_tools;
	/** Current evolution state. */
	get state() {
		return this._state;
	}
	/** Current generation number. */
	get currentGeneration() {
		return this._currentGeneration;
	}
	/** Completed generation results (read-only copy). */
	get history() {
		return this._history;
	}
	config;
	patience;
	threshold;
	escalationThreshold;
	topKAdversarial;
	engine;
	workflowName;
	providedRunId;
	runId = null;
	initPromise = null;
	constructor(config) {
		this.config = config;
		this.patience = config.convergencePatience ?? 50;
		this.threshold = config.convergenceThreshold ?? .001;
		this.escalationThreshold = config.escalationThreshold ?? .85;
		this.topKAdversarial = config.topKAdversarial ?? .05;
		this.engine = config.engine ?? new InMemoryEngine();
		this.workflowName = config.workflowName ?? "evolutionary";
		this.providedRunId = config.runId;
		this._tools = this.buildTools();
	}
	async run(_messages) {
		await this.ensureInitialized();
		if (this._state === "idle") {
			this.startLoop();
			return `Evolution started. Running generation ${this._currentGeneration} of ${this.config.maxGenerations}.`;
		}
		return this.stateSummary();
	}
	/**
	* Open (or re-open) the run with the engine and, on resume, rebuild
	* `history` and `currentGeneration` from the persisted `finalize-*` steps.
	* Idempotent — safe to call multiple times; the work happens once.
	*/
	ensureInitialized() {
		if (this.initPromise) return this.initPromise;
		this.initPromise = (async () => {
			const input = {
				populationSize: this.config.populationSize,
				mutationBatch: this.config.mutationBatch,
				maxGenerations: this.config.maxGenerations,
				paretoObjectives: this.config.paretoObjectives,
				fitnessWeights: this.config.fitnessWeights
			};
			this.runId = await this.engine.startRun(this.workflowName, input, { runId: this.providedRunId });
			const snapshot = await this.engine.getRun(this.runId);
			if (snapshot) {
				const finalized = snapshot.steps.filter((s) => s.stepKey.startsWith("finalize-") && s.status === "completed").map((s) => s.output).sort((a, b) => a.generation - b.generation);
				if (finalized.length > 0) {
					this._history = finalized;
					this._currentGeneration = finalized[finalized.length - 1].generation + 1;
				}
			}
		})();
		return this.initPromise;
	}
	async *stream(messages) {
		yield await this.run(messages);
	}
	collectTools() {
		return this._tools;
	}
	getState() {
		return this._state;
	}
	startLoop() {
		this._state = "running";
		this._loopPromise = (async () => {
			await this.ensureInitialized();
			await this.runLoop();
		})().catch((err) => {
			this._state = "failed";
			console.error("[EvolutionaryAgent] loop crashed:", err);
		});
	}
	pauseLoop() {
		this._state = "paused";
	}
	resumeLoop() {
		if (this._state === "paused" || this._state === "failed") {
			this._state = "running";
			this._loopPromise = (async () => {
				if (this.runId) await this.engine.startRun(this.workflowName, {}, { runId: this.runId });
				await this.runLoop();
			})().catch((err) => {
				this._state = "failed";
				console.error("[EvolutionaryAgent] loop crashed:", err);
			});
		}
	}
	/**
	* Check convergence: returns true when the last `patience` entries in
	* fitnessHistory have a range (max - min) smaller than threshold.
	*/
	checkConvergence(fitnessHistory) {
		if (fitnessHistory.length < this.patience) return false;
		const bests = fitnessHistory.slice(-this.patience).map((r) => r.best);
		return Math.max(...bests) - Math.min(...bests) < this.threshold;
	}
	async runLoop() {
		while (this._state === "running" && this._currentGeneration < this.config.maxGenerations) {
			const gen = this._currentGeneration;
			const timeoutMs = this.config.generationTimeoutMs ?? 6e5;
			const generationTimeout = new Promise((_, reject) => setTimeout(() => reject(/* @__PURE__ */ new Error(`Generation ${gen} timed out after ${timeoutMs}ms`)), timeoutMs));
			try {
				const result = await Promise.race([this.runGeneration(gen), generationTimeout]);
				this._history.push(result);
				this._currentGeneration++;
				if (result.converged) {
					this._state = "converged";
					break;
				}
			} catch (err) {
				console.error(`[EvolutionaryAgent] Generation ${gen} failed:`, err);
				this._currentGeneration++;
				continue;
			}
		}
		if (this._state === "running") this._state = "completed";
		if (this.runId) {
			const status = this._state === "idle" ? "completed" : this._state;
			await this.engine.finishRun(this.runId, status);
		}
	}
	async runGeneration(gen) {
		const startTime = Date.now();
		const runId = this.runId;
		if (!runId) throw new Error("runGeneration called before ensureInitialized");
		const prevGen = gen > 0 ? gen - 1 : 0;
		const parents = await this.engine.step(runId, `load-${gen}`, () => this.config.store.loadTopSurvivors(prevGen, this.config.populationSize, this.config.fitnessWeights));
		let candidates = [];
		if (parents.length > 0) candidates = await this.engine.step(runId, `mutate-${gen}`, () => this.mutate(parents, gen));
		else candidates = await this.engine.step(runId, `seed-${gen}`, () => this.mutate([], gen));
		if (candidates.length === 0) return this.engine.step(runId, `finalize-${gen}`, async () => ({
			generation: gen,
			populationSize: 0,
			bestFitness: 0,
			avgFitness: 0,
			paretoFrontierSize: 0,
			escalated: [],
			wallTimeMs: Date.now() - startTime,
			converged: false
		}));
		const evaluated = await this.engine.step(runId, `evaluate-${gen}`, () => this.evaluate(candidates));
		const judged = await this.engine.step(runId, `judge-${gen}`, () => this.judge(evaluated));
		await this.engine.step(runId, `write-${gen}`, async () => {
			await this.config.store.writeHypotheses(judged);
			return null;
		});
		return this.engine.step(runId, `finalize-${gen}`, async () => {
			const survivors = selectSurvivors(judged, this.config.paretoObjectives, this.config.fitnessWeights, this.config.populationSize);
			const escalated = survivors.filter((h) => compositeFitness(h, this.config.fitnessWeights) >= this.escalationThreshold);
			if (escalated.length > 0) {
				await this.config.store.flagForReview(escalated.map((h) => h.id));
				for (const h of escalated) h.flagged_for_review = true;
			}
			const fitnessHistory = await this.config.store.getFitnessHistory(this.patience, this.config.fitnessWeights);
			const converged = this.checkConvergence(fitnessHistory);
			const scores = survivors.map((h) => compositeFitness(h, this.config.fitnessWeights));
			const bestFitness = scores.length > 0 ? Math.max(...scores) : 0;
			const avgFitness = scores.length > 0 ? scores.reduce((s, v) => s + v, 0) / scores.length : 0;
			const frontier = paretoFrontier(survivors, this.config.paretoObjectives);
			return {
				generation: gen,
				populationSize: survivors.length,
				bestFitness,
				avgFitness,
				paretoFrontierSize: frontier.length,
				escalated,
				wallTimeMs: Date.now() - startTime,
				converged
			};
		});
	}
	async mutate(parents, generation) {
		const payload = {
			parents,
			generation,
			batch_size: this.config.mutationBatch,
			instructions: this.config.instructions
		};
		const response = await this.callAgent(this.config.mutationAgent, payload);
		try {
			if (Array.isArray(response)) return response;
			if (typeof response === "string") return JSON.parse(response);
			if (response && typeof response === "object" && "hypotheses" in response) return response.hypotheses;
		} catch {}
		return [];
	}
	async evaluate(candidates) {
		const evaluated = [...candidates];
		for (const agentUrl of this.config.fitnessAgents) {
			const results = await Promise.allSettled(evaluated.map(async (candidate) => {
				const response = await this.callAgent(agentUrl, { hypothesis: candidate });
				return {
					id: candidate.id,
					scores: response
				};
			}));
			for (let i = 0; i < results.length; i++) {
				const result = results[i];
				if (result.status === "fulfilled") {
					const { scores } = result.value;
					if (scores && typeof scores === "object" && !Array.isArray(scores)) {
						const fitnessUpdate = scores;
						const numericScores = {};
						for (const [k, v] of Object.entries(fitnessUpdate)) if (typeof v === "number") numericScores[k] = v;
						evaluated[i] = {
							...evaluated[i],
							fitness: {
								...evaluated[i].fitness,
								...numericScores
							}
						};
					}
				}
			}
		}
		return evaluated;
	}
	async judge(evaluated) {
		if (!this.config.judgeAgent) return evaluated;
		const sorted = [...evaluated].sort((a, b) => compositeFitness(b, this.config.fitnessWeights) - compositeFitness(a, this.config.fitnessWeights));
		const topCount = Math.max(1, Math.ceil(sorted.length * .2));
		const topCohort = sorted.slice(0, topCount);
		const bottomCohort = sorted.slice(topCount);
		return [...(await Promise.allSettled(topCohort.map(async (candidate) => {
			const response = await this.callAgent(this.config.judgeAgent, { hypothesis: candidate });
			if (response && typeof response === "object" && !Array.isArray(response)) {
				const scores = response;
				const agentEvalScores = {};
				for (const [k, v] of Object.entries(scores)) if (typeof v === "number") agentEvalScores[`agent_eval_${k}`] = v;
				return {
					...candidate,
					fitness: {
						...candidate.fitness,
						...agentEvalScores
					}
				};
			}
			return candidate;
		}))).map((r, i) => r.status === "fulfilled" ? r.value : topCohort[i]), ...bottomCohort];
	}
	async callAgent(url, payload, retries = 3) {
		const body = { input: [{
			role: "user",
			content: JSON.stringify(payload)
		}] };
		const callHeaders = { "Content-Type": "application/json" };
		try {
			callHeaders.Authorization = `Bearer ${await resolveToken()}`;
		} catch (err) {
			console.warn(`[callAgent] No auth token available for ${url}:`, err.message);
		}
		let lastError = null;
		for (let attempt = 0; attempt <= retries; attempt++) {
			if (attempt > 0) {
				const delay = Math.min(1e3 * 2 ** (attempt - 1), 3e4);
				console.warn(`[callAgent] Retry ${attempt}/${retries} for ${url} after ${delay}ms`);
				await new Promise((r) => setTimeout(r, delay));
			}
			let response;
			const controller = new AbortController();
			const timer = setTimeout(() => controller.abort(), 12e4);
			try {
				response = await fetch(`${url}/api/responses`, {
					method: "POST",
					headers: callHeaders,
					body: JSON.stringify(body),
					signal: controller.signal
				});
			} catch (err) {
				clearTimeout(timer);
				lastError = /* @__PURE__ */ new Error(`Agent call to ${url} network error: ${err.message}`);
				continue;
			}
			clearTimeout(timer);
			if (response.status === 502 || response.status === 503 || response.status === 429) {
				const text = await response.text();
				lastError = /* @__PURE__ */ new Error(`Agent call to ${url} failed ${response.status}: ${text.slice(0, 500)}`);
				continue;
			}
			if (!response.ok) {
				const text = await response.text();
				throw new Error(`Agent call to ${url} failed ${response.status}: ${text.slice(0, 500)}`);
			}
			const contentType = response.headers.get("content-type") ?? "";
			if (!contentType.includes("application/json")) {
				const text = await response.text();
				throw new Error(`Agent call to ${url} returned ${contentType || "no content-type"} instead of JSON (likely an auth redirect). Body: ${text.slice(0, 200)}`);
			}
			const data = await response.json();
			let result = data;
			if (data && typeof data === "object" && !Array.isArray(data)) {
				const envelope = data;
				if ("output_text" in envelope && typeof envelope["output_text"] === "string") result = envelope["output_text"];
				else if ("output" in envelope) result = envelope["output"];
				else if ("content" in envelope) result = envelope["content"];
				else if ("result" in envelope) result = envelope["result"];
			}
			if (typeof result === "string") {
				const text = result.trim();
				if (text.startsWith("{") || text.startsWith("[")) try {
					return JSON.parse(text);
				} catch {}
				const fenceMatch = text.match(/```(?:json)?\s*\n?([\s\S]*?)\n?```/);
				if (fenceMatch) try {
					return JSON.parse(fenceMatch[1].trim());
				} catch {}
				const jsonStart = text.search(/[\[{]/);
				if (jsonStart >= 0) try {
					return JSON.parse(text.slice(jsonStart));
				} catch {}
			}
			return result;
		}
		throw lastError ?? /* @__PURE__ */ new Error(`Agent call to ${url} failed after ${retries} retries`);
	}
	buildTools() {
		return [
			defineTool({
				name: "evolution_status",
				description: "Get the current status of the evolutionary loop",
				parameters: z.object({}),
				handler: async () => ({
					state: this._state,
					currentGeneration: this._currentGeneration,
					maxGenerations: this.config.maxGenerations,
					historyLength: this._history.length
				})
			}),
			defineTool({
				name: "best_hypothesis",
				description: "Get the best hypothesis from the most recent completed generation",
				parameters: z.object({}),
				handler: async () => {
					if (this._history.length === 0) return { error: "No generations completed yet" };
					const lastGen = this._currentGeneration > 0 ? this._currentGeneration - 1 : 0;
					return (await this.config.store.loadTopSurvivors(lastGen, 1, this.config.fitnessWeights))[0] ?? { error: "No hypotheses found" };
				}
			}),
			defineTool({
				name: "generation_summary",
				description: "Get a summary of results for a specific generation",
				parameters: z.object({ generation: z.number().int().min(0) }),
				handler: async ({ generation }) => {
					const result = this._history.find((r) => r.generation === generation);
					if (!result) return { error: `No results for generation ${generation}` };
					return result;
				}
			}),
			defineTool({
				name: "pause_evolution",
				description: "Pause the evolutionary loop after the current generation completes",
				parameters: z.object({}),
				handler: async () => {
					this.pauseLoop();
					return {
						success: true,
						state: this._state
					};
				}
			}),
			defineTool({
				name: "resume_evolution",
				description: "Resume the evolutionary loop if it is paused",
				parameters: z.object({}),
				handler: async () => {
					this.resumeLoop();
					return {
						success: true,
						state: this._state
					};
				}
			}),
			defineTool({
				name: "force_escalate",
				description: "Force-escalate the top N hypotheses from the current generation for human review",
				parameters: z.object({ topN: z.number().int().min(1).default(5) }),
				handler: async ({ topN }) => {
					const lastGen = this._currentGeneration > 0 ? this._currentGeneration - 1 : 0;
					const top = await this.config.store.loadTopSurvivors(lastGen, topN, this.config.fitnessWeights);
					if (top.length === 0) return {
						error: "No hypotheses to escalate",
						escalated: []
					};
					await this.config.store.flagForReview(top.map((h) => h.id));
					return {
						escalated: top.map((h) => h.id),
						count: top.length
					};
				}
			})
		];
	}
	stateSummary() {
		const last = this._history[this._history.length - 1];
		const parts = [`State: ${this._state}`, `Generation: ${this._currentGeneration}/${this.config.maxGenerations}`];
		if (last) {
			parts.push(`Best fitness: ${last.bestFitness.toFixed(4)}`);
			parts.push(`Avg fitness: ${last.avgFitness.toFixed(4)}`);
			parts.push(`Pareto frontier size: ${last.paretoFrontierSize}`);
			parts.push(`Escalated: ${last.escalated.length}`);
		}
		return parts.join("\n");
	}
};

//#endregion
//#region src/workflows/population.ts
var PopulationStore = class {
	host;
	populationTable;
	warehouseId;
	chunkSize;
	cacheEnabled;
	cache;
	constructor(config) {
		const rawHost = config.host ?? process.env.DATABRICKS_HOST;
		if (!rawHost) throw new Error("No Databricks host: pass host in config or set DATABRICKS_HOST env var");
		this.host = (rawHost.startsWith("http") ? rawHost : `https://${rawHost}`).replace(/\/$/, "");
		this.populationTable = config.populationTable;
		const wh = config.warehouseId ?? process.env.DATABRICKS_WAREHOUSE_ID;
		if (!wh) throw new Error("No warehouse ID: pass warehouseId in config or set DATABRICKS_WAREHOUSE_ID env var");
		this.warehouseId = wh;
		this.chunkSize = config.chunkSize ?? 25;
		this.cacheEnabled = config.cacheEnabled ?? true;
		this.cache = /* @__PURE__ */ new Map();
	}
	async writeHypotheses(hypotheses) {
		if (hypotheses.length === 0) return;
		if (hypotheses.length > 0) console.log(`[PopStore] writing ${hypotheses.length} hypotheses, first fitness: ${JSON.stringify(hypotheses[0].fitness)}`);
		for (let i = 0; i < hypotheses.length; i += this.chunkSize) {
			const valuesList = hypotheses.slice(i, i + this.chunkSize).map((h) => {
				return `('${esc$1(safeName$1(h.id, "hypothesis.id"))}', ${h.generation}, '${h.parent_id ? esc$1(safeName$1(h.parent_id, "hypothesis.parent_id")) : ""}', '${esc$1(JSON.stringify(h.fitness))}', '${esc$1(JSON.stringify(h.metadata))}', ${h.flagged_for_review ? "true" : "false"}, current_timestamp())`;
			});
			const statement = `INSERT INTO ${this.populationTable} (id, generation, parent_id, fitness, metadata, flagged_for_review, created_at) VALUES ${valuesList.join(", ")}`;
			await this.executeSql(statement);
		}
		this.cache.clear();
	}
	async updateFitnessScores(updates) {
		for (const { id, fitness } of updates) {
			const escapedId = esc$1(safeName$1(id, "hypothesis.id"));
			const escapedFitness = esc$1(JSON.stringify(fitness));
			const statement = `MERGE INTO ${this.populationTable} AS target USING (SELECT '${escapedId}' AS id, '${escapedFitness}' AS fitness) AS source ON target.id = source.id WHEN MATCHED THEN UPDATE SET target.fitness = source.fitness`;
			await this.executeSql(statement);
		}
		this.cache.clear();
	}
	async flagForReview(ids) {
		if (ids.length === 0) return;
		const idList = ids.map((id) => `'${esc$1(safeName$1(id, "hypothesis.id"))}'`).join(", ");
		const statement = `UPDATE ${this.populationTable} SET flagged_for_review = true WHERE id IN (${idList})`;
		await this.executeSql(statement);
		this.cache.clear();
	}
	async loadGeneration(generation) {
		if (this.cacheEnabled && this.cache.has(generation)) return this.cache.get(generation);
		const statement = `
      WITH ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY created_at DESC) AS rn
        FROM ${this.populationTable}
        WHERE generation = ${generation}
      )
      SELECT * FROM ranked WHERE rn = 1`;
		const response = await this.executeSql(statement);
		const hypotheses = this.parseRows(response);
		if (this.cacheEnabled) this.cache.set(generation, hypotheses);
		return hypotheses;
	}
	async loadTopSurvivors(generation, topN, weights) {
		return (await this.loadGeneration(generation)).slice().sort((a, b) => compositeFitness(b, weights) - compositeFitness(a, weights)).slice(0, topN);
	}
	async getFitnessHistory(nGenerations, weights) {
		const maxGen = `(SELECT MAX(generation) FROM ${this.populationTable})`;
		const statement = `
      WITH ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY created_at DESC) AS rn
        FROM ${this.populationTable}
        WHERE generation > ${maxGen} - ${Math.max(1, Math.floor(nGenerations))}
      )
      SELECT * FROM ranked WHERE rn = 1`;
		const response = await this.executeSql(statement);
		const allHypotheses = this.parseRows(response);
		const byGen = /* @__PURE__ */ new Map();
		for (const h of allHypotheses) {
			const existing = byGen.get(h.generation) ?? [];
			existing.push(h);
			byGen.set(h.generation, existing);
		}
		return Array.from(byGen.keys()).sort((a, b) => a - b).map((gen) => {
			const scores = byGen.get(gen).map((h) => compositeFitness(h, weights));
			return {
				generation: gen,
				best: scores.length > 0 ? Math.max(...scores) : 0,
				avg: scores.length > 0 ? scores.reduce((s, v) => s + v, 0) / scores.length : 0
			};
		}).slice(-nGenerations);
	}
	async getActiveConstraints() {
		const statement = `SELECT * FROM ${`${this.populationTable}_review_queue`} WHERE status = 'approved'`;
		const response = await this.executeSql(statement);
		return this.parseRows(response).map((h) => h);
	}
	clearCache() {
		this.cache.clear();
	}
	async executeSql(statement) {
		const token = await resolveToken();
		const url = `${this.host}/api/2.0/sql/statements/`;
		const body = {
			statement,
			warehouse_id: this.warehouseId,
			wait_timeout: "30s",
			on_wait_timeout: "CANCEL",
			disposition: "INLINE",
			format: "JSON_ARRAY"
		};
		const res = await fetch(url, {
			method: "POST",
			headers: {
				Authorization: `Bearer ${token}`,
				"Content-Type": "application/json"
			},
			body: JSON.stringify(body)
		});
		if (!res.ok) {
			const text = await res.text();
			throw new Error(`Databricks SQL API ${res.status}: ${text}`);
		}
		return res.json();
	}
	parseRows(response) {
		const columns = response.manifest?.schema?.columns ?? [];
		return (response.result?.data_array ?? []).map((row) => {
			const obj = {};
			columns.forEach((col, i) => {
				obj[col.name] = row[i] ?? null;
			});
			return {
				id: obj["id"] ?? "",
				generation: Number(obj["generation"] ?? 0),
				parent_id: obj["parent_id"] || null,
				fitness: JSON.parse(obj["fitness"] ?? "{}"),
				metadata: JSON.parse(obj["metadata"] ?? "{}"),
				flagged_for_review: obj["flagged_for_review"] === "true",
				created_at: obj["created_at"] ?? ""
			};
		});
	}
};
/** Escape values for inline SQL strings. Handles single quotes and backslashes. */
function esc$1(s) {
	return s.replace(/\\/g, "\\\\").replace(/'/g, "''");
}
/** Reject values containing obvious SQL injection patterns. */
function safeName$1(s, label) {
	if (s.length > 1e3) throw new Error(`${label} too long (${s.length} chars)`);
	if (/;\s*(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|EXEC|UNION)\b/i.test(s)) throw new Error(`Suspicious SQL pattern in ${label}`);
	return s;
}

//#endregion
//#region src/workflows/engine-delta.ts
/**
* DeltaEngine — durable WorkflowEngine backed by Delta tables via the
* Databricks SQL Statements API.
*
* Stores run metadata in `{tablePrefix}_runs` and step records in
* `{tablePrefix}_steps`. Tables are created lazily on first use via
* `CREATE TABLE IF NOT EXISTS`. Step results are JSON-serialized.
*
* Reuses the same auth path (`resolveToken()`) and SQL transport pattern
* as `PopulationStore`, so OBO / M2M token resolution works identically.
*
* Per-process cache for `step()` lookups keeps replays inside one run
* cheap. Cross-process race on the same (runId, stepKey) is possible but
* rare; MERGE is used on writes to keep that case idempotent.
*/
var DeltaEngine = class {
	host;
	warehouseId;
	runsTable;
	stepsTable;
	cacheEnabled;
	stepCache = /* @__PURE__ */ new Map();
	bootstrapPromise = null;
	constructor(config) {
		const rawHost = config.host ?? process.env.DATABRICKS_HOST;
		if (!rawHost) throw new Error("No Databricks host: pass host in config or set DATABRICKS_HOST env var");
		this.host = (rawHost.startsWith("http") ? rawHost : `https://${rawHost}`).replace(/\/$/, "");
		const wh = config.warehouseId ?? process.env.DATABRICKS_WAREHOUSE_ID;
		if (!wh) throw new Error("No warehouse ID: pass warehouseId in config or set DATABRICKS_WAREHOUSE_ID env var");
		this.warehouseId = wh;
		this.runsTable = `${config.tablePrefix}_runs`;
		this.stepsTable = `${config.tablePrefix}_steps`;
		this.cacheEnabled = config.cacheEnabled ?? true;
	}
	async startRun(workflowName, input, opts) {
		await this.bootstrap();
		const runId = opts?.runId ?? randomId();
		const inputJson = esc(JSON.stringify(input ?? null));
		const escWorkflow = esc(safeName(workflowName, "workflowName"));
		const escRunId = esc(safeName(runId, "runId"));
		const statement = `
      MERGE INTO ${this.runsTable} AS target
      USING (SELECT
        '${escRunId}' AS run_id,
        '${escWorkflow}' AS workflow_name,
        '${inputJson}' AS input
      ) AS source
      ON target.run_id = source.run_id
      WHEN MATCHED THEN UPDATE SET
        target.status = 'running',
        target.updated_at = current_timestamp()
      WHEN NOT MATCHED THEN INSERT (
        run_id, workflow_name, status, input, started_at, updated_at
      ) VALUES (
        source.run_id, source.workflow_name, 'running', source.input,
        current_timestamp(), current_timestamp()
      )
    `;
		await this.executeSql(statement);
		return runId;
	}
	async step(runId, stepKey, handler) {
		await this.bootstrap();
		const cached = await this.lookupStep(runId, stepKey);
		if (cached) {
			if (cached.status === "completed") return cached.output;
			throw new StepFailedError(stepKey, cached.error ?? "step failed");
		}
		const startMs = Date.now();
		try {
			const result = await handler();
			const record = {
				stepKey,
				status: "completed",
				output: result,
				durationMs: Date.now() - startMs,
				recordedAt: (/* @__PURE__ */ new Date()).toISOString()
			};
			await this.persistStep(runId, record);
			if (this.cacheEnabled) this.stepCache.set(cacheKey(runId, stepKey), record);
			return result;
		} catch (err) {
			const record = {
				stepKey,
				status: "failed",
				error: err instanceof Error ? err.message : String(err),
				durationMs: Date.now() - startMs,
				recordedAt: (/* @__PURE__ */ new Date()).toISOString()
			};
			await this.persistStep(runId, record);
			if (this.cacheEnabled) this.stepCache.set(cacheKey(runId, stepKey), record);
			throw err;
		}
	}
	async finishRun(runId, status, output) {
		await this.bootstrap();
		const setOutput = output === void 0 ? "" : `, output = '${esc(JSON.stringify(output))}'`;
		const statement = `
      UPDATE ${this.runsTable}
      SET status = '${esc(safeName(status, "status"))}'${setOutput}, updated_at = current_timestamp()
      WHERE run_id = '${esc(safeName(runId, "runId"))}'
    `;
		await this.executeSql(statement);
	}
	async getRun(runId) {
		await this.bootstrap();
		const runRows = parseRows(await this.executeSql(`SELECT run_id, workflow_name, status, input, output, started_at, updated_at FROM ${this.runsTable} WHERE run_id = '${esc(safeName(runId, "runId"))}'`));
		if (runRows.length === 0) return null;
		const row = runRows[0];
		const stepRows = parseRows(await this.executeSql(`SELECT step_key, status, output, error, duration_ms, recorded_at FROM ${this.stepsTable} WHERE run_id = '${esc(safeName(runId, "runId"))}'`));
		return {
			runId: row["run_id"] ?? runId,
			workflowName: row["workflow_name"] ?? "",
			status: row["status"] ?? "running",
			input: parseJsonOrNull(row["input"]),
			output: row["output"] === null || row["output"] === void 0 ? void 0 : parseJsonOrNull(row["output"]),
			startedAt: row["started_at"] ?? "",
			updatedAt: row["updated_at"] ?? "",
			steps: stepRows.map((s) => ({
				stepKey: s["step_key"] ?? "",
				status: s["status"] ?? "completed",
				output: s["output"] === null || s["output"] === void 0 ? void 0 : parseJsonOrNull(s["output"]),
				error: s["error"] ?? void 0,
				durationMs: Number(s["duration_ms"] ?? 0),
				recordedAt: s["recorded_at"] ?? ""
			}))
		};
	}
	async listRuns(filter) {
		await this.bootstrap();
		const conditions = [];
		if (filter?.workflowName) conditions.push(`workflow_name = '${esc(safeName(filter.workflowName, "workflowName"))}'`);
		if (filter?.status) conditions.push(`status = '${esc(safeName(filter.status, "status"))}'`);
		const where = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";
		const limit = filter?.limit !== void 0 ? `LIMIT ${Math.max(0, Math.floor(filter.limit))}` : "";
		return parseRows(await this.executeSql(`SELECT run_id, workflow_name, status, started_at, updated_at FROM ${this.runsTable} ${where} ORDER BY started_at DESC ${limit}`)).map((r) => ({
			runId: r["run_id"] ?? "",
			workflowName: r["workflow_name"] ?? "",
			status: r["status"] ?? "running",
			startedAt: r["started_at"] ?? "",
			updatedAt: r["updated_at"] ?? ""
		}));
	}
	/** Drop all in-process caches. Useful for tests. */
	clearCache() {
		this.stepCache.clear();
	}
	bootstrap() {
		if (this.bootstrapPromise) return this.bootstrapPromise;
		this.bootstrapPromise = (async () => {
			await this.executeSql(`
        CREATE TABLE IF NOT EXISTS ${this.runsTable} (
          run_id STRING NOT NULL,
          workflow_name STRING NOT NULL,
          status STRING NOT NULL,
          input STRING,
          output STRING,
          started_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL
        ) USING DELTA
      `);
			await this.executeSql(`
        CREATE TABLE IF NOT EXISTS ${this.stepsTable} (
          run_id STRING NOT NULL,
          step_key STRING NOT NULL,
          status STRING NOT NULL,
          output STRING,
          error STRING,
          duration_ms BIGINT,
          recorded_at TIMESTAMP NOT NULL
        ) USING DELTA
      `);
		})();
		return this.bootstrapPromise;
	}
	async lookupStep(runId, stepKey) {
		const ck = cacheKey(runId, stepKey);
		if (this.cacheEnabled) {
			const hit = this.stepCache.get(ck);
			if (hit) return hit;
		}
		const rows = parseRows(await this.executeSql(`SELECT step_key, status, output, error, duration_ms, recorded_at FROM ${this.stepsTable} WHERE run_id = '${esc(safeName(runId, "runId"))}' AND step_key = '${esc(safeName(stepKey, "stepKey"))}' LIMIT 1`));
		if (rows.length === 0) return null;
		const r = rows[0];
		const record = {
			stepKey: r["step_key"] ?? stepKey,
			status: r["status"] ?? "completed",
			output: r["output"] === null || r["output"] === void 0 ? void 0 : parseJsonOrNull(r["output"]),
			error: r["error"] ?? void 0,
			durationMs: Number(r["duration_ms"] ?? 0),
			recordedAt: r["recorded_at"] ?? ""
		};
		if (this.cacheEnabled) this.stepCache.set(ck, record);
		return record;
	}
	async persistStep(runId, record) {
		const outputJson = record.output === void 0 ? "NULL" : `'${esc(JSON.stringify(record.output))}'`;
		const errorVal = record.error === void 0 ? "NULL" : `'${esc(record.error)}'`;
		const statement = `
      MERGE INTO ${this.stepsTable} AS target
      USING (SELECT
        '${esc(safeName(runId, "runId"))}' AS run_id,
        '${esc(safeName(record.stepKey, "stepKey"))}' AS step_key,
        '${esc(safeName(record.status, "status"))}' AS status,
        ${outputJson} AS output,
        ${errorVal} AS error,
        ${record.durationMs} AS duration_ms
      ) AS source
      ON target.run_id = source.run_id AND target.step_key = source.step_key
      WHEN NOT MATCHED THEN INSERT (
        run_id, step_key, status, output, error, duration_ms, recorded_at
      ) VALUES (
        source.run_id, source.step_key, source.status, source.output,
        source.error, source.duration_ms, current_timestamp()
      )
    `;
		await this.executeSql(statement);
	}
	async executeSql(statement) {
		const token = await resolveToken();
		const url = `${this.host}/api/2.0/sql/statements/`;
		const body = {
			statement,
			warehouse_id: this.warehouseId,
			wait_timeout: "30s",
			on_wait_timeout: "CANCEL",
			disposition: "INLINE",
			format: "JSON_ARRAY"
		};
		const res = await fetch(url, {
			method: "POST",
			headers: {
				Authorization: `Bearer ${token}`,
				"Content-Type": "application/json"
			},
			body: JSON.stringify(body)
		});
		if (!res.ok) {
			const text = await res.text();
			throw new Error(`Databricks SQL API ${res.status}: ${text}`);
		}
		return res.json();
	}
};
/** Escape values for inline SQL strings. Handles single quotes and backslashes. */
function esc(s) {
	return s.replace(/\\/g, "\\\\").replace(/'/g, "''");
}
/** Reject values containing obvious SQL injection patterns. */
function safeName(s, label) {
	if (s.length > 1e3) throw new Error(`${label} too long (${s.length} chars)`);
	if (/;\s*(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|EXEC|UNION)\b/i.test(s)) throw new Error(`Suspicious SQL pattern in ${label}`);
	return s;
}
function cacheKey(runId, stepKey) {
	return `${runId}::${stepKey}`;
}
function randomId() {
	return `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
function parseJsonOrNull(s) {
	if (s === null || s === void 0 || s === "") return null;
	try {
		return JSON.parse(s);
	} catch {
		return s;
	}
}
function parseRows(response) {
	const columns = response.manifest?.schema?.columns ?? [];
	return (response.result?.data_array ?? []).map((row) => {
		const obj = {};
		columns.forEach((col, i) => {
			obj[col.name] = row[i] ?? null;
		});
		return obj;
	});
}

//#endregion
//#region src/workflows/engine-inngest.ts
var InngestEngine = class {
	step$;
	runs = /* @__PURE__ */ new Map();
	constructor(step) {
		this.step$ = step;
	}
	async startRun(workflowName, input, opts) {
		const runId = opts?.runId ?? `inngest-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
		const now = (/* @__PURE__ */ new Date()).toISOString();
		const existing = this.runs.get(runId);
		if (existing) {
			existing.status = "running";
			existing.updatedAt = now;
		} else this.runs.set(runId, {
			workflowName,
			status: "running",
			startedAt: now,
			updatedAt: now,
			input
		});
		return runId;
	}
	async step(_runId, stepKey, handler) {
		return this.step$.run(stepKey, handler);
	}
	async finishRun(runId, status, output) {
		const run = this.runs.get(runId);
		if (!run) return;
		run.status = status;
		if (output !== void 0) run.output = output;
		run.updatedAt = (/* @__PURE__ */ new Date()).toISOString();
	}
	async getRun(runId) {
		const run = this.runs.get(runId);
		if (!run) return null;
		return {
			runId,
			workflowName: run.workflowName,
			status: run.status,
			input: run.input,
			output: run.output,
			startedAt: run.startedAt,
			updatedAt: run.updatedAt,
			steps: []
		};
	}
	async listRuns(filter) {
		let results = Array.from(this.runs.entries()).map(([runId, r]) => ({
			runId,
			workflowName: r.workflowName,
			status: r.status,
			startedAt: r.startedAt,
			updatedAt: r.updatedAt
		}));
		if (filter?.workflowName) results = results.filter((r) => r.workflowName === filter.workflowName);
		if (filter?.status) results = results.filter((r) => r.status === filter.status);
		if (filter?.limit !== void 0) results = results.slice(0, filter.limit);
		return results;
	}
};

//#endregion
//#region src/eval/predict.ts
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
function createPredictFn(url, options = {}) {
	const endpoint = `${url.replace(/\/$/, "")}/responses`;
	const headers = { "Content-Type": "application/json" };
	if (options.token) headers["Authorization"] = `Bearer ${options.token}`;
	return async function predict(input) {
		const payload = typeof input === "string" ? { input } : { input: input.messages.map((m) => ({
			role: m.role,
			content: m.content
		})) };
		const res = await fetch(endpoint, {
			method: "POST",
			headers,
			body: JSON.stringify(payload)
		});
		if (!res.ok) {
			const text = await res.text();
			throw new Error(`Predict request failed (${res.status}): ${text}`);
		}
		const data = await res.json();
		if (typeof data.output_text === "string") return data.output_text;
		const text = data.output?.[0]?.content?.[0]?.text;
		if (typeof text === "string") return text;
		throw new Error(`Unexpected response shape: ${JSON.stringify(data)}`);
	};
}

//#endregion
//#region src/eval/harness.ts
/**
* Run all eval cases against the given predict function.
*
* Cases are executed in batches respecting the concurrency limit.
* Latency is measured per case. Pass/fail is determined by simple
* string inclusion of `expected` in the output.
*
* @example
* const predict = createPredictFn('http://localhost:8000');
* const summary = await runEval(predict, [
*   { input: 'What is 2+2?', expected: '4' },
*   { input: 'Capital of France?', expected: 'Paris' },
* ]);
* console.log(`Passed: ${summary.passed}/${summary.total}`);
*/
async function runEval(predictFn, cases, options = {}) {
	const concurrency = options.concurrency ?? 5;
	const verbose = options.verbose ?? false;
	const results = [];
	for (let i = 0; i < cases.length; i += concurrency) {
		const batch = cases.slice(i, i + concurrency);
		const batchResults = await Promise.all(batch.map(async (evalCase) => {
			const start = Date.now();
			try {
				const output = await predictFn(evalCase.input);
				const latency_ms = Date.now() - start;
				const passed = evalCase.expected !== void 0 ? output.includes(evalCase.expected) : void 0;
				const result = {
					input: evalCase.input,
					output,
					expected: evalCase.expected,
					passed,
					latency_ms
				};
				if (verbose) {
					const status = passed === void 0 ? "N/A" : passed ? "PASS" : "FAIL";
					process.stdout.write(`[${status}] ${latency_ms}ms — ${evalCase.input.slice(0, 60)}\n`);
				}
				return result;
			} catch (err) {
				const latency_ms = Date.now() - start;
				const error = err instanceof Error ? err.message : String(err);
				if (verbose) process.stdout.write(`[ERR] ${latency_ms}ms — ${evalCase.input.slice(0, 60)}: ${error}\n`);
				return {
					input: evalCase.input,
					output: "",
					expected: evalCase.expected,
					passed: false,
					latency_ms,
					error
				};
			}
		}));
		results.push(...batchResults);
	}
	const total = results.length;
	const errored = results.filter((r) => r.error !== void 0).length;
	const withExpected = results.filter((r) => r.expected !== void 0);
	const passed = withExpected.filter((r) => r.passed === true).length;
	const failed = withExpected.filter((r) => r.passed === false).length;
	const avg_latency_ms = total > 0 ? Math.round(results.reduce((sum, r) => sum + r.latency_ms, 0) / total) : 0;
	const summary = {
		total,
		passed,
		failed,
		errored,
		avg_latency_ms,
		results
	};
	if (verbose) process.stdout.write(`\nSummary: ${total} cases | ${passed} passed | ${failed} failed | ${errored} errored | avg ${avg_latency_ms}ms\n`);
	return summary;
}

//#endregion
//#region src/genie.ts
/**
* genieTool — wrap a Genie space as a registered apx-agent tool.
*
* @example
* import { genieTool } from 'appkit-agent';
*
* const agent = createAgentPlugin({
*   model: 'databricks-claude-sonnet-4-6',
*   tools: [genieTool('abc123', { description: 'Answer sales data questions' })],
* });
*/
const TERMINAL_STATUSES = new Set([
	"COMPLETED",
	"FAILED",
	"CANCELLED"
]);
const POLL_INTERVAL_MS = 2e3;
const MAX_POLLS = 30;
async function queryGenie(host, token, spaceId, question) {
	const { conversation_id: convId, message_id: msgId } = await dbFetch(`${host}/api/2.0/genie/spaces/${spaceId}/start_conversation`, {
		token,
		method: "POST",
		body: { content: question }
	});
	let msgResp = { status: "" };
	for (let i = 0; i < MAX_POLLS; i++) {
		msgResp = await dbFetch(`${host}/api/2.0/genie/spaces/${spaceId}/conversations/${convId}/messages/${msgId}`, {
			token,
			method: "GET"
		});
		if (TERMINAL_STATUSES.has(msgResp.status)) break;
		await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
	}
	if (msgResp.status === "FAILED" || msgResp.status === "CANCELLED") return `Genie query ${msgResp.status.toLowerCase()}.`;
	for (const att of msgResp.attachments ?? []) if (att.text?.content) return att.text.content;
	return "";
}
/**
* Create an AgentTool that queries a Genie space by natural-language conversation.
*
* @param spaceId - Genie space ID (the UUID from the Genie space URL).
* @param opts    - Optional overrides for name, description, host, and auth headers.
*/
function genieTool(spaceId, opts = {}) {
	return defineTool({
		name: opts.name ?? "ask_genie",
		description: opts.description ?? `Ask a natural-language question to the Genie space and receive an answer. Use this for data questions that Genie can answer via SQL. (spaceId=${spaceId})`,
		parameters: z.object({ question: z.string().describe("The question to ask Genie") }),
		handler: async ({ question }) => {
			return queryGenie(resolveHost(opts.host), await resolveToken(opts.oboHeaders), spaceId, question);
		}
	});
}

//#endregion
//#region src/catalog.ts
/**
* catalogTool, lineageTool, schemaTool — Unity Catalog tool factories.
*
* @example
* import { catalogTool, lineageTool, schemaTool } from 'appkit-agent';
*
* createAgentPlugin({
*   tools: [
*     catalogTool('main', 'sales'),
*     lineageTool(),
*     schemaTool(),
*   ],
* });
*/
function toSqlLiteral(value, typeName) {
	if (value === null || value === void 0) return "NULL";
	const t = typeName.toUpperCase();
	if (t === "BOOLEAN") return value ? "TRUE" : "FALSE";
	if ([
		"STRING",
		"CHAR",
		"VARCHAR",
		"TEXT"
	].includes(t)) return `'${String(value).replace(/'/g, "''")}'`;
	const n = Number(value);
	if (!isNaN(n)) return String(value);
	return `'${String(value).replace(/'/g, "''")}'`;
}
/**
* Create a tool that lists tables in a Unity Catalog schema.
*
* The LLM calls this tool with no arguments — the catalog and schema are
* baked in at construction time.
*
* @param catalog - UC catalog name.
* @param schema  - Schema name within the catalog.
* @param opts    - Optional name, description, host, and auth overrides.
*/
function catalogTool(catalog, schema, opts = {}) {
	return defineTool({
		name: opts.name ?? "list_tables",
		description: opts.description ?? `List all tables in ${catalog}.${schema} with their names and descriptions.`,
		parameters: z.object({}),
		handler: async () => {
			const host = resolveHost(opts.host);
			const token = await resolveToken(opts.oboHeaders);
			return ((await dbFetch(`${host}/api/2.1/unity-catalog/tables?catalog_name=${encodeURIComponent(catalog)}&schema_name=${encodeURIComponent(schema)}`, {
				token,
				method: "GET"
			})).tables ?? []).map((t) => ({
				name: t.name,
				full_name: t.full_name,
				table_type: t.table_type ?? "",
				comment: t.comment ?? ""
			}));
		}
	});
}
/**
* Create a tool that fetches upstream/downstream lineage for a UC table.
*
* The LLM provides `table_name` as a fully qualified name: `catalog.schema.table`.
*
* @param opts - Optional name, description, host, and auth overrides.
*/
function lineageTool(opts = {}) {
	return defineTool({
		name: opts.name ?? "get_table_lineage",
		description: opts.description ?? "Get the upstream sources and downstream consumers for a Unity Catalog table. Pass the full table name as catalog.schema.table_name.",
		parameters: z.object({ table_name: z.string().describe("Full table name: catalog.schema.table") }),
		handler: async ({ table_name }) => {
			const host = resolveHost(opts.host);
			const token = await resolveToken(opts.oboHeaders);
			const data = await dbFetch(`${host}/api/2.1/unity-catalog/lineage-tracking/table-lineage?table_name=${encodeURIComponent(table_name)}`, {
				token,
				method: "GET"
			});
			return {
				table: table_name,
				upstreams: (data.upstreams ?? []).filter((u) => u.tableInfo?.name).map((u) => ({
					full_name: u.tableInfo.name,
					table_type: u.tableInfo.table_type ?? ""
				})),
				downstreams: (data.downstreams ?? []).filter((d) => d.tableInfo?.name).map((d) => ({
					full_name: d.tableInfo.name,
					table_type: d.tableInfo.table_type ?? ""
				}))
			};
		}
	});
}
/**
* Create a tool that describes the columns of a Unity Catalog table.
*
* The LLM provides `table_name` as a fully qualified name: `catalog.schema.table`.
*
* @param opts - Optional name, description, host, and auth overrides.
*/
function schemaTool(opts = {}) {
	return defineTool({
		name: opts.name ?? "describe_table",
		description: opts.description ?? "Describe the columns of a Unity Catalog table — names, types, and descriptions. Pass the full table name as catalog.schema.table_name.",
		parameters: z.object({ table_name: z.string().describe("Full table name: catalog.schema.table") }),
		handler: async ({ table_name }) => {
			const host = resolveHost(opts.host);
			const token = await resolveToken(opts.oboHeaders);
			return ((await dbFetch(`${host}/api/2.1/unity-catalog/tables/${encodeURIComponent(table_name)}`, {
				token,
				method: "GET"
			})).columns ?? []).map((col) => ({
				name: col.name,
				type: col.type_name ?? "",
				type_text: col.type_text ?? "",
				comment: col.comment ?? "",
				nullable: col.nullable ?? true,
				position: col.position ?? 0
			}));
		}
	});
}
async function resolveWarehouseId(host, token, warehouseId) {
	if (warehouseId) return warehouseId;
	const warehouses = (await dbFetch(`${host}/api/2.0/sql/warehouses`, {
		token,
		method: "GET"
	})).warehouses ?? [];
	const serverless = warehouses.find((w) => w.warehouse_type?.toLowerCase().includes("serverless"));
	const first = warehouses.find((w) => w.id);
	const id = (serverless ?? first)?.id;
	if (!id) throw new Error("No SQL warehouse available in this workspace");
	return id;
}
async function executeSqlStatement(host, token, warehouseId, statement) {
	const data = await dbFetch(`${host}/api/2.0/sql/statements/`, {
		token,
		method: "POST",
		body: {
			statement,
			warehouse_id: warehouseId,
			wait_timeout: "30s",
			disposition: "INLINE",
			format: "JSON_ARRAY"
		}
	});
	if (data.status.state !== "SUCCEEDED") throw new Error(`SQL failed: ${data.status.error?.message ?? data.status.state}`);
	const cols = data.manifest?.schema?.columns ?? [];
	return (data.result?.data_array ?? []).map((row) => Object.fromEntries(cols.map((col, i) => [col.name, row[i] ?? null])));
}
/**
* Create a tool that executes a Unity Catalog function via SQL.
*
* The function definition is fetched from UC on the first call and cached —
* parameter names, types, and order are derived automatically.
*
* @param functionName - Fully qualified UC function name: `catalog.schema.function`.
* @param opts         - Optional overrides for name, description, host, warehouseId, and auth.
*
* @example
* ucFunctionTool('main.tools.classify_intent', {
*   description: 'Classify user intent. params: {text, min_confidence}',
* })
*/
function ucFunctionTool(functionName, opts = {}) {
	const shortName = functionName.split(".").pop() ?? functionName;
	const name = opts.name ?? shortName;
	const description = opts.description ?? `Execute the Unity Catalog function \`${functionName}\`. Pass parameters as a JSON object with parameter names as keys, e.g. {"param1": "value1", "param2": 42}.`;
	let funcDef = null;
	return defineTool({
		name,
		description,
		parameters: z.object({ params: z.record(z.string(), z.unknown()).describe("Function parameters as {param_name: value} pairs") }),
		handler: async ({ params }) => {
			const host = resolveHost(opts.host);
			const token = await resolveToken(opts.oboHeaders);
			if (!funcDef) {
				const info = await dbFetch(`${host}/api/2.1/unity-catalog/functions/${encodeURIComponent(functionName)}`, {
					token,
					method: "GET"
				});
				funcDef = {
					data_type: info.data_type ?? "",
					parameters: (info.input_params?.parameters ?? []).sort((a, b) => (a.position ?? 0) - (b.position ?? 0)).map((p) => ({
						name: p.name,
						position: p.position ?? 0,
						type_name: p.type_name ?? "STRING"
					}))
				};
			}
			const sqlArgs = funcDef.parameters.map((p) => toSqlLiteral(params[p.name], p.type_name));
			const sql = sqlArgs.length === 0 ? `SELECT ${functionName}()` : `SELECT ${functionName}(${sqlArgs.join(", ")})`;
			const rows = await executeSqlStatement(host, token, await resolveWarehouseId(host, token, opts.warehouseId), sql);
			if (rows.length === 1 && Object.keys(rows[0]).length === 1) return Object.values(rows[0])[0];
			return rows;
		}
	});
}

//#endregion
//#region src/connectors/lakebase.ts
/**
* Lakebase connector — typed tools for the SQL Statement Execution API.
*
* Provides three tool factories:
*   - createLakebaseQueryTool       SELECT with parameterized filters
*   - createLakebaseMutateTool      INSERT / UPDATE / DELETE
*   - createLakebaseSchemaInspectTool  information_schema.columns query
*/
/**
* Extract a Databricks token from OBO headers or the environment.
*/
/**
* POST a SQL statement to the Databricks SQL Statement Execution API and
* return the response.
*/
async function executeSql(host, token, catalog, schema, statement, params) {
	const url = `${host}/api/2.0/sql/statements/`;
	const body = {
		statement,
		catalog,
		schema,
		wait_timeout: "30s",
		on_wait_timeout: "CANCEL",
		disposition: "INLINE",
		format: "JSON_ARRAY"
	};
	if (params && params.length > 0) body.parameters = params.map((p) => ({
		name: p.name,
		value: p.value,
		type: p.type
	}));
	const res = await fetch(url, {
		method: "POST",
		headers: {
			Authorization: `Bearer ${token}`,
			"Content-Type": "application/json"
		},
		body: JSON.stringify(body)
	});
	if (!res.ok) {
		const text = await res.text();
		throw new Error(`Databricks SQL API ${res.status}: ${text}`);
	}
	return res.json();
}
/**
* Convert a SQL Statements API response (column names + data_array) into an
* array of plain row objects.
*/
function rowsToObjects(response) {
	const columns = response.manifest?.schema?.columns ?? [];
	return (response.result?.data_array ?? []).map((row) => {
		const obj = {};
		columns.forEach((col, i) => {
			obj[col.name] = row[i] ?? null;
		});
		return obj;
	});
}
/**
* Create a Lakebase query tool that executes SELECT statements with
* optional parameterized filters.
*/
function createLakebaseQueryTool(config) {
	const host = resolveHost(config.host);
	const { catalog, schema } = config;
	return defineTool({
		name: "lakebase_query",
		description: `Query rows from a table in the ${catalog}.${schema} schema using parameterized SELECT statements.`,
		parameters: z.object({
			table: z.string().describe("Table name (unqualified — catalog.schema are taken from config)"),
			columns: z.array(z.string()).optional().describe("Columns to select; defaults to *"),
			filters: z.record(z.string(), z.unknown()).optional().describe("Key-value filter pairs for WHERE clause"),
			limit: z.number().int().min(1).optional().describe("Maximum rows to return (default 100)")
		}),
		handler: async ({ table, columns, filters, limit }) => {
			const token = await resolveToken();
			const fqn = `${catalog}.${schema}.${table}`;
			const cols = columns && columns.length > 0 ? columns.join(", ") : "*";
			const effectiveLimit = limit ?? 100;
			const { clause, params } = filters && Object.keys(filters).length > 0 ? buildSqlParams(filters) : {
				clause: "",
				params: []
			};
			return rowsToObjects(await executeSql(host, token, catalog, schema, `SELECT ${cols} FROM ${fqn}${clause ? ` WHERE ${clause}` : ""} LIMIT ${effectiveLimit}`, params));
		}
	});
}
/**
* Create a Lakebase mutate tool that executes INSERT, UPDATE, or DELETE
* statements.
*/
function createLakebaseMutateTool(config) {
	const host = resolveHost(config.host);
	const { catalog, schema } = config;
	return defineTool({
		name: "lakebase_mutate",
		description: `Insert, update, or delete rows in a table in the ${catalog}.${schema} schema.`,
		parameters: z.object({
			table: z.string().describe("Table name (unqualified)"),
			operation: z.enum([
				"INSERT",
				"UPDATE",
				"DELETE"
			]).describe("DML operation to perform"),
			values: z.record(z.string(), z.unknown()).optional().describe("Column-value pairs for INSERT or UPDATE SET"),
			filters: z.record(z.string(), z.unknown()).optional().describe("Key-value filter pairs for WHERE clause (required for UPDATE and DELETE)")
		}),
		handler: async ({ table, operation, values, filters }) => {
			const token = await resolveToken();
			const fqn = `${catalog}.${schema}.${table}`;
			let statement;
			let params = [];
			if (operation === "INSERT") {
				if (!values || Object.keys(values).length === 0) throw new Error("INSERT requires values");
				const cols = Object.keys(values).join(", ");
				const placeholders = Object.keys(values).map((k) => `:${k}`).join(", ");
				const { params: insertParams } = buildSqlParams(values);
				params = insertParams;
				statement = `INSERT INTO ${fqn} (${cols}) VALUES (${placeholders})`;
			} else if (operation === "UPDATE") {
				if (!values || Object.keys(values).length === 0) throw new Error("UPDATE requires values");
				if (!filters || Object.keys(filters).length === 0) throw new Error("UPDATE requires filters to avoid updating all rows");
				const setCols = Object.keys(values).map((k) => `${k} = :set_${k}`).join(", ");
				const setPrefixed = {};
				for (const [k, v] of Object.entries(values)) setPrefixed[`set_${k}`] = v;
				const { params: setParams } = buildSqlParams(setPrefixed);
				const { clause: whereClause, params: filterParams } = buildSqlParams(filters);
				params = [...setParams, ...filterParams];
				statement = `UPDATE ${fqn} SET ${setCols} WHERE ${whereClause}`;
			} else {
				if (!filters || Object.keys(filters).length === 0) throw new Error("DELETE requires filters to avoid deleting all rows");
				const { clause: whereClause, params: filterParams } = buildSqlParams(filters);
				params = filterParams;
				statement = `DELETE FROM ${fqn} WHERE ${whereClause}`;
			}
			return {
				success: true,
				statement_id: (await executeSql(host, token, catalog, schema, statement, params)).statement_id
			};
		}
	});
}
/**
* Create a Lakebase schema inspect tool that queries information_schema.columns
* for the configured catalog.schema.
*/
function createLakebaseSchemaInspectTool(config) {
	const host = resolveHost(config.host);
	const { catalog, schema } = config;
	return defineTool({
		name: "lakebase_schema_inspect",
		description: `Inspect column definitions in ${catalog}.${schema} via information_schema.columns.`,
		parameters: z.object({ table_filter: z.string().optional().describe("Optional table name to filter results to a single table") }),
		handler: async ({ table_filter }) => {
			const token = await resolveToken();
			const params = [{
				name: "cat",
				value: catalog,
				type: "STRING"
			}, {
				name: "sch",
				value: schema,
				type: "STRING"
			}];
			let statement = `SELECT * FROM information_schema.columns WHERE table_catalog = :cat AND table_schema = :sch`;
			if (table_filter) {
				params.push({
					name: "tbl",
					value: table_filter,
					type: "STRING"
				});
				statement += ` AND table_name = :tbl`;
			}
			statement += ` ORDER BY table_name, ordinal_position`;
			return rowsToObjects(await executeSql(host, token, catalog, schema, statement, params));
		}
	});
}

//#endregion
//#region src/connectors/vector-search.ts
/**
* Vector Search connector tools.
*
* Provides three agent tools backed by the Databricks Vector Search REST API:
*  - vs_query   — similarity search over an index
*  - vs_upsert  — add or update a vector record
*  - vs_delete  — delete records by primary key
*/
/**
* Create a similarity-search tool for a Vector Search index.
* Requires `config.vectorSearchIndex` to be set.
*/
function createVSQueryTool(config) {
	if (!config.vectorSearchIndex) throw new Error("vectorSearchIndex is required in ConnectorConfig for createVSQueryTool");
	const indexName = config.vectorSearchIndex;
	return defineTool({
		name: "vs_query",
		description: "Run a similarity search against a Databricks Vector Search index.",
		parameters: z.object({
			query_text: z.string().describe("The text to search for"),
			filters: z.record(z.string(), z.unknown()).optional().describe("Optional key-value filters"),
			num_results: z.number().int().min(1).optional().describe("Number of results to return (default 10)")
		}),
		handler: async ({ query_text, filters, num_results }) => {
			const host = resolveHost(config.host);
			const token = await resolveToken();
			const body = {
				query_text,
				num_results: num_results ?? 10,
				columns: []
			};
			if (filters !== void 0) body.filters_json = JSON.stringify(filters);
			const response = await dbFetch(`${host}/api/2.0/vector-search/indexes/${encodeURIComponent(indexName)}/query`, {
				token,
				method: "POST",
				body
			});
			const columnNames = response.manifest.columns.map((c) => c.name);
			return response.result.data_array.map((row) => {
				const obj = {};
				columnNames.forEach((col, i) => {
					obj[col] = row[i];
				});
				return obj;
			});
		}
	});
}
/**
* Create an upsert tool for a Vector Search index.
* Requires `config.vectorSearchIndex` to be set.
*/
function createVSUpsertTool(config) {
	if (!config.vectorSearchIndex) throw new Error("vectorSearchIndex is required in ConnectorConfig for createVSUpsertTool");
	const indexName = config.vectorSearchIndex;
	return defineTool({
		name: "vs_upsert",
		description: "Add or update a vector record in a Databricks Vector Search index.",
		parameters: z.object({
			id: z.string().describe("Primary key for the record"),
			text: z.string().describe("Text content for the vector embedding"),
			metadata: z.record(z.string(), z.unknown()).optional().describe("Optional additional metadata fields")
		}),
		handler: async ({ id, text, metadata }) => {
			const host = resolveHost(config.host);
			const token = await resolveToken();
			const record = {
				id,
				text,
				...metadata
			};
			await dbFetch(`${host}/api/2.0/vector-search/indexes/${encodeURIComponent(indexName)}/upsert-data`, {
				token,
				method: "POST",
				body: { inputs_json: JSON.stringify([record]) }
			});
			return {
				success: true,
				id
			};
		}
	});
}
/**
* Create a delete tool for a Vector Search index.
* Requires `config.vectorSearchIndex` to be set.
*/
function createVSDeleteTool(config) {
	if (!config.vectorSearchIndex) throw new Error("vectorSearchIndex is required in ConnectorConfig for createVSDeleteTool");
	const indexName = config.vectorSearchIndex;
	return defineTool({
		name: "vs_delete",
		description: "Delete records from a Databricks Vector Search index by primary key.",
		parameters: z.object({ ids: z.array(z.string()).describe("List of primary key values to delete") }),
		handler: async ({ ids }) => {
			const host = resolveHost(config.host);
			const token = await resolveToken();
			await dbFetch(`${host}/api/2.0/vector-search/indexes/${encodeURIComponent(indexName)}/delete-data`, {
				token,
				method: "POST",
				body: { primary_keys: ids }
			});
			return {
				success: true,
				deleted: ids.length
			};
		}
	});
}

//#endregion
//#region src/connectors/doc-parser.ts
/**
* Doc Parser connector tools.
*
* Provides utilities and agent tools for uploading documents, chunking text,
* and extracting entities via LLM from document content stored in UC Volumes.
*
*  - chunkText             — pure function: split text into overlapping chunks
*  - createDocUploadTool   — upload a document to a UC Volume via Files API
*  - createDocChunkTool    — chunk text using schema-configured chunk settings
*  - createDocExtractEntitiesTool — extract entities from chunks via FMAPI
*/
/**
* Split `text` into overlapping chunks.
*
* @param text         - Input text to chunk
* @param chunkSize    - Maximum characters per chunk
* @param chunkOverlap - Number of characters to overlap between consecutive chunks
* @returns Array of Chunk objects with sequential chunk_ids and byte positions
*/
function chunkText(text, chunkSize, chunkOverlap) {
	if (!text) return [];
	const step = chunkOverlap >= chunkSize ? chunkSize : chunkSize - chunkOverlap;
	const chunks = [];
	let position = 0;
	let index = 0;
	while (position < text.length) {
		const slice = text.slice(position, position + chunkSize);
		chunks.push({
			chunk_id: `chunk_${index}`,
			text: slice,
			position
		});
		index++;
		position += step;
	}
	return chunks;
}
/**
* Create a tool that uploads a document to a UC Volume via the Files API.
* Requires `config.volumePath` to be set.
*/
function createDocUploadTool(config) {
	if (!config.volumePath) throw new Error("volumePath is required in ConnectorConfig for createDocUploadTool");
	const volumePath = config.volumePath;
	return defineTool({
		name: "doc_upload",
		description: "Upload a document to a Unity Catalog Volume via the Databricks Files API.",
		parameters: z.object({
			filename: z.string().describe("Name for the file in the volume"),
			content: z.string().describe("File content to upload")
		}),
		handler: async ({ filename, content }) => {
			const host = resolveHost(config.host);
			const token = await resolveToken();
			const docId = randomUUID();
			const remotePath = `${volumePath.replace(/\/$/, "")}/${docId}_${filename}`;
			const url = `${host}/api/2.0/fs/files${remotePath}`;
			const res = await fetch(url, {
				method: "PUT",
				headers: {
					Authorization: `Bearer ${token}`,
					"Content-Type": "application/octet-stream"
				},
				body: content
			});
			if (!res.ok) {
				const text = await res.text();
				throw new Error(`Files API PUT ${res.status}: ${text}`);
			}
			return {
				doc_id: docId,
				path: remotePath,
				filename,
				size: content.length
			};
		}
	});
}
/**
* Create a tool that splits text into chunks using schema-configured settings.
*/
function createDocChunkTool(config) {
	return defineTool({
		name: "doc_chunk",
		description: "Split document text into overlapping chunks for downstream processing.",
		parameters: z.object({ text: z.string().describe("Text content to split into chunks") }),
		handler: async ({ text }) => {
			return chunkText(text, config.entitySchema?.extraction.chunk_size ?? 1e3, config.entitySchema?.extraction.chunk_overlap ?? 200);
		}
	});
}
/**
* Create a tool that extracts entities from text chunks using an LLM via FMAPI.
* Requires `config.entitySchema` to be set.
*/
function createDocExtractEntitiesTool(config) {
	return defineTool({
		name: "doc_extract_entities",
		description: "Extract structured entities from document chunks using an LLM.",
		parameters: z.object({
			chunks: z.array(z.object({
				chunk_id: z.string(),
				text: z.string()
			})).describe("Array of text chunks to extract entities from"),
			model: z.string().optional().describe("Model to use for extraction (default: databricks-claude-sonnet-4-6)")
		}),
		handler: async ({ chunks, model }) => {
			const host = resolveHost(config.host);
			const token = await resolveToken();
			const modelName = model ?? "databricks-claude-sonnet-4-6";
			const schema = config.entitySchema;
			const promptTemplate = schema?.extraction.prompt_template ?? "";
			const entityNames = (schema?.entities ?? []).map((e) => e.name).join(", ");
			const entityFields = (schema?.entities ?? []).flatMap((e) => e.fields.map((f) => f.name)).join(", ");
			const allEntities = [];
			for (const chunk of chunks) {
				const prompt = promptTemplate.replace(/\{entity_names\}/g, entityNames).replace(/\{entity_fields\}/g, entityFields).replace(/\{chunk_text\}/g, chunk.text);
				const url = `${host}/serving-endpoints/chat/completions`;
				const res = await fetch(url, {
					method: "POST",
					headers: {
						Authorization: `Bearer ${token}`,
						"Content-Type": "application/json"
					},
					body: JSON.stringify({
						model: modelName,
						messages: [{
							role: "user",
							content: prompt
						}]
					})
				});
				if (!res.ok) {
					const text = await res.text();
					throw new Error(`FMAPI POST ${res.status}: ${text}`);
				}
				const content = (await res.json()).choices?.[0]?.message?.content ?? "";
				try {
					const cleaned = content.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
					const parsed = JSON.parse(cleaned);
					if (Array.isArray(parsed)) for (const entity of parsed) allEntities.push({
						...entity,
						_chunk_id: chunk.chunk_id
					});
				} catch {}
			}
			return allEntities;
		}
	});
}

//#endregion
export { AgentState, DeltaEngine, EvolutionaryAgent, HandoffAgent, InMemoryEngine, InMemorySessionStore, InngestEngine, LoopAgent, ParallelAgent, PopulationStore, RemoteAgent, RouterAgent, SequentialAgent, Session, StepFailedError, addSpan, buildSqlParams, catalogTool, chunkText, compositeFitness, createAgentPlugin, createDevPlugin, createDiscoveryPlugin, createDocChunkTool, createDocExtractEntitiesTool, createDocUploadTool, createHypothesis, createLakebaseMutateTool, createLakebaseQueryTool, createLakebaseSchemaInspectTool, createMcpPlugin, createPredictFn, createTrace, createVSDeleteTool, createVSQueryTool, createVSUpsertTool, dbFetch, defineTool, endSpan, endTrace, genieTool, getDefaultSessionStore, getMcpAuth, getRequestContext, getTrace, getTraces, initDatabricksClient, lineageTool, mcpAuthStore, paretoDominates, paretoFrontier, parseEntitySchema, resolveHost, resolveToken, runEval, runViaSDK, runWithContext, schemaTool, selectSurvivors, setDefaultSessionStore, storeTrace, streamViaSDK, toFunctionTool, toStrictSchema, toSubAgentTool, toolsToFunctionSchemas, truncate, ucFunctionTool, zodToJsonSchema };
//# sourceMappingURL=index.mjs.map