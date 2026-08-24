import { describe, expect, it } from 'vitest';
import {
  evaluateOrderedServicePolicies,
  orderedPolicies,
  validateServicePolicies,
  type ServicePolicy,
} from '../src/service-policies.js';

const policies: ServicePolicy[] = [
  { name: 'late', kind: 'builtin', builtin: 'unsafe_content', rank: 20 },
  { name: 'early', kind: 'builtin', builtin: 'sensitive_data', rank: 10 },
  { name: 'result', kind: 'builtin', builtin: 'hallucination', phase: 'on_result', rank: 1 },
];

describe('service policies', () => {
  it('validates declarations and applies defaults', () => {
    expect(validateServicePolicies({
      attachments: [{ name: 'mcp', target_type: 'mcp_service', target: 'orders', policies }],
    })).toMatchObject({ local_mode: 'mirror', native_mode: 'off', attachments: [{ mode: 'enforce' }] });
  });

  it('rejects invalid kind references and unknown fields', () => {
    expect(() => validateServicePolicies({ attachments: [{ name: 'x', target_type: 'mcp_service', target: 'x', policies: [{ name: 'p', kind: 'sql', prompt: 'no' }] }] })).toThrow();
    expect(() => validateServicePolicies({ unexpected: true })).toThrow(/unknown field/);
  });

  it('orders on_call ascending and on_result descending', () => {
    expect(orderedPolicies(policies, 'on_call').map((policy) => policy.name)).toEqual(['early', 'late']);
    expect(orderedPolicies(policies, 'on_result').map((policy) => policy.name)).toEqual(['result']);
  });

  it('short-circuits at the first deny', () => {
    const evaluated: string[] = [];
    const decision = evaluateOrderedServicePolicies(policies, { phase: 'on_call', target_type: 'mcp_service', target: 'orders' }, {
      mode: 'enforce',
      adapter: 'local',
      evaluator: (policy) => {
        evaluated.push(policy.name);
        return policy.name === 'early' ? 'DENY' : 'ALLOW';
      },
    });
    expect(decision.action).toBe('DENY');
    expect(evaluated).toEqual(['early']);
  });

  it('fails closed in enforce and records unavailable in dry_run', () => {
    const event = { phase: 'on_call', target_type: 'mcp_service' as const, target: 'orders' };
    expect(evaluateOrderedServicePolicies(policies, event, { mode: 'enforce', adapter: 'local', evaluator: () => { throw new Error('judge down'); } }).action).toBe('DENY');
    expect(evaluateOrderedServicePolicies(policies, event, { mode: 'dry_run', adapter: 'local', evaluator: () => { throw new Error('judge down'); } }).action).toBe('UNAVAILABLE');
  });

  it('only permits ASK for MCP on_call events', () => {
    const decision = evaluateOrderedServicePolicies(policies, { phase: 'on_result', target_type: 'model_service', target: 'model' }, {
      mode: 'enforce',
      adapter: 'local',
      evaluator: () => 'ASK',
    });
    expect(decision.action).toBe('DENY');
  });
});
