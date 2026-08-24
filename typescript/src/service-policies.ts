/** Portable Service Policy declarations and deterministic local decisions. */

export type ServicePolicyTargetType =
  | 'mcp_service'
  | 'model_service'
  | 'model_provider_service'
  | 'agent_service';
export type ServicePolicyKind = 'builtin' | 'llm_judge' | 'sql';
export type BuiltinServicePolicy = 'sensitive_data' | 'unsafe_content' | 'jailbreak' | 'hallucination';
export type ServicePolicyPhase = 'on_call' | 'on_result' | 'both';
export type ServicePolicyMode = 'enforce' | 'dry_run';
export type LocalPolicyMode = 'mirror' | 'off';
export type NativePolicyMode = 'off' | 'plan' | 'apply' | 'required';
export type ServicePolicyAction = 'ALLOW' | 'ASK' | 'DENY' | 'UNAVAILABLE';

export interface ServicePolicy {
  name: string;
  kind: ServicePolicyKind;
  builtin?: BuiltinServicePolicy;
  classifier?: string;
  prompt?: string;
  function?: string;
  phase?: ServicePolicyPhase;
  rank?: number;
}

export interface ServicePolicyAttachment {
  name: string;
  target_type: ServicePolicyTargetType;
  target: string;
  mode?: ServicePolicyMode;
  policies: ServicePolicy[];
}

export interface AbacSelector {
  tags: Record<string, string>;
}

export interface ServicePoliciesConfig {
  local_mode?: LocalPolicyMode;
  native_mode?: NativePolicyMode;
  attachments?: ServicePolicyAttachment[];
  abac?: AbacSelector;
}

export interface ServicePolicyEvent {
  phase: ServicePolicyPhase | string;
  target_type: ServicePolicyTargetType;
  target: string;
  content?: string;
  tool_name?: string;
  arguments?: Record<string, unknown>;
  result?: unknown;
  context?: Record<string, unknown>;
}

export interface ServicePolicyDecision {
  action: ServicePolicyAction;
  reason: string | null;
  policy_name: string | null;
  phase: string;
  rank: number | null;
  mode: ServicePolicyMode;
  adapter: 'local' | 'native';
}

export type ServicePolicyEvaluationResult =
  | ServicePolicyAction
  | { action: ServicePolicyAction | string; reason?: string | null }
  | null
  | undefined;

export type ServicePolicyEvaluator = (
  policy: ServicePolicy,
  event: ServicePolicyEvent,
) => ServicePolicyEvaluationResult;

const TARGET_TYPES: ReadonlySet<string> = new Set([
  'mcp_service',
  'model_service',
  'model_provider_service',
  'agent_service',
]);
const KINDS: ReadonlySet<string> = new Set(['builtin', 'llm_judge', 'sql']);
const BUILTINS: ReadonlySet<string> = new Set([
  'sensitive_data',
  'unsafe_content',
  'jailbreak',
  'hallucination',
]);
const PHASES: ReadonlySet<string> = new Set(['on_call', 'on_result', 'both']);
const MODES: ReadonlySet<string> = new Set(['enforce', 'dry_run']);
const LOCAL_MODES: ReadonlySet<string> = new Set(['mirror', 'off']);
const NATIVE_MODES: ReadonlySet<string> = new Set(['off', 'plan', 'apply', 'required']);

const keys = (value: Record<string, unknown>): Set<string> => new Set(Object.keys(value));

function assertObject(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function assertKeys(value: Record<string, unknown>, allowed: ReadonlySet<string>, label: string): void {
  for (const key of keys(value)) {
    if (!allowed.has(key)) throw new TypeError(`${label} has unknown field ${key}`);
  }
}

function stringValue(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.trim() === '') throw new TypeError(`${label} must be non-empty`);
  return value;
}

function enumValue<T extends string>(value: unknown, allowed: ReadonlySet<string>, fallback: T, label: string): T {
  const selected = value ?? fallback;
  if (typeof selected !== 'string' || !allowed.has(selected)) throw new TypeError(`${label} is invalid`);
  return selected as T;
}

function normalizePolicy(value: unknown): ServicePolicy {
  const raw = assertObject(value, 'service policy');
  assertKeys(raw, new Set(['name', 'kind', 'builtin', 'classifier', 'prompt', 'function', 'phase', 'rank']), 'service policy');
  if (raw.kind === undefined) throw new TypeError('service policy kind is required');
  const kind = enumValue<ServicePolicyKind>(raw.kind, KINDS, 'builtin', 'service policy kind');
  const phase = enumValue<ServicePolicyPhase>(raw.phase, PHASES, 'on_call', 'service policy phase');
  const rank = raw.rank ?? 100;
  if (typeof rank !== 'number' || !Number.isInteger(rank) || rank < 0) throw new TypeError('service policy rank must be a non-negative integer');
  const policy: ServicePolicy = { name: stringValue(raw.name, 'service policy name'), kind, phase, rank };

  if (kind === 'builtin') {
    if (raw.builtin === undefined) throw new TypeError('builtin policies require builtin');
    policy.builtin = enumValue<BuiltinServicePolicy>(raw.builtin, BUILTINS, 'sensitive_data', 'builtin policy');
    if (policy.builtin === 'jailbreak' && phase !== 'on_call') throw new TypeError('jailbreak policies are request/on_call only');
    if (policy.builtin === 'hallucination' && phase !== 'on_result') throw new TypeError('hallucination policies are result/on_result only');
    if (raw.classifier !== undefined || raw.prompt !== undefined || raw.function !== undefined) throw new TypeError('builtin policies cannot define classifier, prompt, or function');
  } else if (kind === 'llm_judge') {
    policy.classifier = stringValue(raw.classifier, 'llm_judge classifier');
    policy.prompt = stringValue(raw.prompt, 'llm_judge prompt');
    if (raw.builtin !== undefined || raw.function !== undefined) throw new TypeError('llm_judge policies cannot define builtin or function');
  } else {
    policy.function = stringValue(raw.function, 'sql function');
    if (raw.builtin !== undefined || raw.classifier !== undefined || raw.prompt !== undefined) throw new TypeError('sql policies cannot define builtin, classifier, or prompt');
  }
  return policy;
}

/** Validate and apply the same defaults as the Python declaration models. */
export function validateServicePolicies(value: unknown): ServicePoliciesConfig & {
  local_mode: LocalPolicyMode;
  native_mode: NativePolicyMode;
  attachments: ServicePolicyAttachment[];
} {
  const raw = assertObject(value, 'service policies');
  assertKeys(raw, new Set(['local_mode', 'native_mode', 'attachments', 'abac']), 'service policies');
  const local_mode = enumValue<LocalPolicyMode>(raw.local_mode, LOCAL_MODES, 'mirror', 'local_mode');
  const native_mode = enumValue<NativePolicyMode>(raw.native_mode, NATIVE_MODES, 'off', 'native_mode');
  const attachmentsValue = raw.attachments ?? [];
  if (!Array.isArray(attachmentsValue)) throw new TypeError('service policy attachments must be an array');
  const attachments = attachmentsValue.map((value) => {
    const attachment = assertObject(value, 'service policy attachment');
    assertKeys(attachment, new Set(['name', 'target_type', 'target', 'mode', 'policies']), 'service policy attachment');
    const target_type = enumValue<ServicePolicyTargetType>(attachment.target_type, TARGET_TYPES, 'mcp_service', 'service policy target_type');
    const mode = enumValue<ServicePolicyMode>(attachment.mode, MODES, 'enforce', 'service policy mode');
    const policiesValue = attachment.policies ?? [];
    if (!Array.isArray(policiesValue)) throw new TypeError('service policy attachment policies must be an array');
    return {
      name: stringValue(attachment.name, 'service policy attachment name'),
      target_type,
      target: stringValue(attachment.target, 'service policy target'),
      mode,
      policies: policiesValue.map(normalizePolicy),
    } satisfies ServicePolicyAttachment;
  });
  if (native_mode === 'required' && attachments.length === 0) throw new TypeError("native_mode='required' needs at least one attachment");
  let abac: AbacSelector | undefined;
  if (raw.abac !== undefined) {
    const abacRaw = assertObject(raw.abac, 'service policy abac');
    assertKeys(abacRaw, new Set(['tags']), 'service policy abac');
    const tagsRaw = assertObject(abacRaw.tags ?? {}, 'service policy abac tags');
    const tags: Record<string, string> = {};
    for (const [key, tag] of Object.entries(tagsRaw)) tags[stringValue(key, 'ABAC tag key')] = stringValue(tag, 'ABAC tag value');
    abac = { tags };
  }
  return { local_mode, native_mode, attachments, abac };
}

/** Return applicable policies in deterministic rank order. */
export function orderedPolicies(policies: readonly ServicePolicy[], phase: ServicePolicyPhase | string): ServicePolicy[] {
  const requested = enumValue<ServicePolicyPhase>(phase, PHASES, 'on_call', 'service policy event phase');
  const applicable = policies.filter((policy) => {
    const policyPhase = policy.phase ?? 'on_call';
    return requested === 'both' || policyPhase === requested || policyPhase === 'both';
  });
  return [...applicable].sort((left, right) => {
    const leftRank = left.rank ?? 100;
    const rightRank = right.rank ?? 100;
    return requested === 'on_result' ? rightRank - leftRank : leftRank - rightRank;
  });
}

function normalizeAction(value: ServicePolicyEvaluationResult): { action: ServicePolicyAction; reason: string | null } {
  const raw = typeof value === 'string' ? { action: value } : value;
  if (!raw) return { action: 'ALLOW', reason: null };
  const action = String(raw.action).toUpperCase() as ServicePolicyAction;
  if (!new Set(['ALLOW', 'ASK', 'DENY', 'UNAVAILABLE']).has(action)) throw new TypeError(`unknown policy action ${raw.action}`);
  return { action, reason: raw.reason ?? null };
}

/** Evaluate in rank order, stopping at the first ASK or DENY and failing closed. */
export function evaluateOrderedServicePolicies(
  policies: readonly ServicePolicy[],
  event: ServicePolicyEvent,
  options: { mode: ServicePolicyMode; adapter: 'local' | 'native'; evaluator?: ServicePolicyEvaluator },
): ServicePolicyDecision {
  const mode = options.mode;
  for (const policy of orderedPolicies(policies, event.phase)) {
    let result: { action: ServicePolicyAction; reason: string | null };
    try {
      result = normalizeAction(options.evaluator?.(policy, event));
    } catch (error) {
      result = {
        action: mode === 'dry_run' ? 'UNAVAILABLE' : 'DENY',
        reason: `policy evaluation unavailable (${String(error)})`,
      };
    }
    if (result.action === 'ASK' && (event.target_type !== 'mcp_service' || event.phase !== 'on_call')) {
      result = {
        action: mode === 'dry_run' ? 'UNAVAILABLE' : 'DENY',
        reason: result.reason ?? 'ASK is supported only for MCP on_call policies',
      };
    }
    if (result.action !== 'ALLOW') {
      return {
        action: result.action,
        reason: result.reason,
        policy_name: policy.name,
        phase: event.phase,
        rank: policy.rank ?? 100,
        mode,
        adapter: options.adapter,
      };
    }
  }
  return { action: 'ALLOW', reason: null, policy_name: null, phase: event.phase, rank: null, mode, adapter: options.adapter };
}
