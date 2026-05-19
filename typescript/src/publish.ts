/**
 * publish_to_supervisor — register an apx-agent as a Mosaic AI Supervisor sub-agent.
 *
 * Closes the multi-agent topology loop: once an apx-agent is deployed as a
 * Model Serving endpoint, this module registers it as a tool on an existing
 * Supervisor Agent so the supervisor can route to it alongside Knowledge
 * Assistants, Genie spaces, and other sub-agents.
 *
 * The Mosaic AI Supervisor Agent SDK lives at
 * `databricks.sdk.service.supervisoragents` (Python) and is currently in
 * preview. There is no equivalent typed surface in the Databricks JS SDK, so
 * callers inject a `SupervisorAgentsClient` matching the shape below.
 *
 * Two entry points:
 *   * `createSupervisorAgent(...)` — create a new Supervisor Agent.
 *   * `publishToSupervisor(...)` — add a serving-endpoint-backed sub-agent
 *     to an existing Supervisor Agent.
 *
 * ## SupervisorAgentsClient interface
 *
 * Mirrors the `databricks.sdk.service.supervisoragents` shape (preview API):
 *
 * ```ts
 * interface SupervisorAgentsClient {
 *   createSupervisorAgent(opts: {supervisorAgent: SupervisorAgent}): Promise<SupervisorAgent>;
 *   createTool(opts: {parent: string, tool: SupervisorTool, toolId: string}): Promise<SupervisorTool>;
 * }
 * ```
 *
 * The preview surface may evolve. `SupervisorTool` exposes an `extra` slot for
 * unmodelled wire fields (matches Python's `extra_tool_kwargs` escape hatch).
 *
 * ## Wire format note
 *
 * The Supervisor SDK uses snake-case wire fields like `tool_type`. The TS
 * interface uses camelCase (`toolType`) for ergonomics; the wire transport
 * layer is responsible for snake/camel translation (matching how the
 * Databricks JS SDK handles other services).
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A Mosaic AI Supervisor Agent record (preview SDK shape). */
export interface SupervisorAgent {
  readonly displayName: string;
  readonly description: string;
  readonly instructions: string;
  readonly id?: string;
}

/**
 * A Supervisor Agent tool record (preview SDK shape).
 *
 * `extra` is an escape hatch for wire fields this helper doesn't model
 * explicitly — matches Python's `extra_tool_kwargs`. Use it if the preview
 * SDK adds fields before this helper is updated.
 */
export interface SupervisorTool {
  readonly toolType: string;
  readonly name: string;
  readonly description: string;
  readonly id?: string;
  readonly extra?: Record<string, unknown>;
}

/**
 * SDK-like client surface — callers inject a client matching this shape.
 *
 * Mirrors `databricks.sdk.service.supervisoragents` (preview).
 */
export interface SupervisorAgentsClient {
  createSupervisorAgent(opts: {
    supervisorAgent: SupervisorAgent;
  }): Promise<SupervisorAgent>;
  createTool(opts: {
    parent: string;
    tool: SupervisorTool;
    toolId: string;
  }): Promise<SupervisorTool>;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/** Normalise a string into a safe tool_id (letters / digits / underscores). */
export function _slug(text: string): string {
  const cleaned = text
    .replace(/[^a-zA-Z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toLowerCase();
  return cleaned || 'tool';
}

// ---------------------------------------------------------------------------
// createSupervisorAgent
// ---------------------------------------------------------------------------

const CreateSupervisorAgentOpts = z.object({
  displayName: z.string().min(1),
  description: z.string(),
  instructions: z.string(),
  supervisorAgentsClient: z.custom<SupervisorAgentsClient>(
    (v) =>
      typeof v === 'object' &&
      v !== null &&
      'createSupervisorAgent' in v &&
      'createTool' in v,
    'supervisorAgentsClient must implement createSupervisorAgent/createTool',
  ),
});

/**
 * Create a Mosaic AI Supervisor Agent.
 *
 * @param opts.displayName - Human-readable name shown in the Databricks UI.
 * @param opts.description - Short description for the supervisor's metadata.
 * @param opts.instructions - System prompt for the supervisor's routing behavior.
 * @param opts.supervisorAgentsClient - Injected client matching the SDK shape.
 * @returns The created supervisor agent record.
 */
export async function createSupervisorAgent(opts: {
  displayName: string;
  description: string;
  instructions: string;
  supervisorAgentsClient: SupervisorAgentsClient;
}): Promise<SupervisorAgent> {
  const { displayName, description, instructions, supervisorAgentsClient } =
    CreateSupervisorAgentOpts.parse(opts);

  return supervisorAgentsClient.createSupervisorAgent({
    supervisorAgent: {
      displayName,
      description,
      instructions,
    },
  });
}

// ---------------------------------------------------------------------------
// publishToSupervisor
// ---------------------------------------------------------------------------

const PublishToSupervisorOpts = z.object({
  supervisorAgentId: z.string().min(1),
  servingEndpoint: z.string().min(1),
  description: z.string(),
  displayName: z.string().optional(),
  toolId: z.string().optional(),
  supervisorAgentsClient: z.custom<SupervisorAgentsClient>(
    (v) =>
      typeof v === 'object' &&
      v !== null &&
      'createSupervisorAgent' in v &&
      'createTool' in v,
    'supervisorAgentsClient must implement createSupervisorAgent/createTool',
  ),
  extraToolKwargs: z.record(z.string(), z.unknown()).optional(),
});

/**
 * Register a serving-endpoint-backed agent as a Supervisor sub-agent.
 *
 * This is how a deployed apx-agent becomes routable from a Supervisor. The
 * supervisor's LLM picks among its declared sub-agents at runtime; when it
 * picks the one you publish here, the call flows to the serving endpoint with
 * identity passthrough scoped to the endpoint's declared resources.
 *
 * @param opts.supervisorAgentId - ID of the existing Supervisor Agent.
 * @param opts.servingEndpoint - Name of the Model Serving endpoint hosting the sub-agent.
 * @param opts.description - How the supervisor's LLM should think about routing here.
 * @param opts.displayName - Defaults to `servingEndpoint`.
 * @param opts.toolId - Stable identifier; defaults to a slug of `servingEndpoint`.
 * @param opts.supervisorAgentsClient - Injected client matching the SDK shape.
 * @param opts.extraToolKwargs - Escape hatch for unmodelled preview fields.
 * @returns The created Tool record.
 */
export async function publishToSupervisor(opts: {
  supervisorAgentId: string;
  servingEndpoint: string;
  description: string;
  displayName?: string;
  toolId?: string;
  supervisorAgentsClient: SupervisorAgentsClient;
  extraToolKwargs?: Record<string, unknown>;
}): Promise<SupervisorTool> {
  const {
    supervisorAgentId,
    servingEndpoint,
    description,
    toolId,
    supervisorAgentsClient,
    extraToolKwargs,
  } = PublishToSupervisorOpts.parse(opts);

  const finalToolId = toolId || _slug(servingEndpoint);

  const tool: SupervisorTool = {
    toolType: 'serving_endpoint',
    name: servingEndpoint,
    description,
    ...(extraToolKwargs ? { extra: extraToolKwargs } : {}),
  };

  return supervisorAgentsClient.createTool({
    parent: `supervisor-agents/${supervisorAgentId}`,
    tool,
    toolId: finalToolId,
  });
}
