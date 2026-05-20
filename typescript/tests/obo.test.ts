/**
 * Tests for obo.ts — unified OBO header extractor (TS port of
 * python/tests/test_obo.py).
 *
 * Load-bearing properties:
 *   - customInputs.user_token beats X-Forwarded-Access-Token (caller wins)
 *   - customInputs.workspace_host beats X-Forwarded-Host beats DATABRICKS_HOST env
 *   - case-insensitive + hyphen/underscore-tolerant header lookup
 *   - accepts both Headers (web standard) and plain dicts
 *   - missing fields are omitted (never set to empty string)
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { extractOboHeaders } from '../src/obo.js';

const ORIGINAL_ENV = process.env;

beforeEach(() => {
  // Snapshot env so per-test mutations don't leak.
  process.env = { ...ORIGINAL_ENV };
  delete process.env.DATABRICKS_HOST;
});

afterEach(() => {
  process.env = ORIGINAL_ENV;
});

describe('extractOboHeaders — basic shape', () => {
  it('returns {} when no inputs are given', () => {
    expect(extractOboHeaders()).toEqual({});
  });

  it('reads user_token + workspace_host from customInputs', () => {
    const out = extractOboHeaders({
      customInputs: { user_token: 'tok-1', workspace_host: 'wks.databricks.com' },
    });
    expect(out).toEqual({ userToken: 'tok-1', workspaceHost: 'wks.databricks.com' });
  });

  it('reads X-Forwarded-Access-Token + X-Forwarded-Host from a plain dict', () => {
    const out = extractOboHeaders({
      headers: {
        'x-forwarded-access-token': 'hdr-tok',
        'x-forwarded-host': 'hdr-host',
      },
    });
    expect(out.userToken).toBe('hdr-tok');
    expect(out.workspaceHost).toBe('hdr-host');
  });

  it('reads user_id + user_email from customInputs', () => {
    const out = extractOboHeaders({
      customInputs: { user_id: 'u-42', user_email: 'alice@example.com' },
    });
    expect(out).toEqual({ userId: 'u-42', userEmail: 'alice@example.com' });
  });

  it('reads user_id + user_email from headers', () => {
    const out = extractOboHeaders({
      headers: {
        'x-forwarded-user': 'u-42',
        'x-forwarded-email': 'alice@example.com',
      },
    });
    expect(out.userId).toBe('u-42');
    expect(out.userEmail).toBe('alice@example.com');
  });
});

describe('extractOboHeaders — precedence rules', () => {
  it('customInputs.user_token wins over X-Forwarded-Access-Token', () => {
    const out = extractOboHeaders({
      customInputs: { user_token: 'caller-wins' },
      headers: { 'x-forwarded-access-token': 'proxy-loses' },
    });
    expect(out.userToken).toBe('caller-wins');
  });

  it('customInputs.workspace_host beats X-Forwarded-Host beats env', () => {
    process.env.DATABRICKS_HOST = 'env-host';
    const out = extractOboHeaders({
      customInputs: { workspace_host: 'ci-host' },
      headers: { 'x-forwarded-host': 'hdr-host' },
    });
    expect(out.workspaceHost).toBe('ci-host');

    const out2 = extractOboHeaders({
      headers: { 'x-forwarded-host': 'hdr-host' },
    });
    expect(out2.workspaceHost).toBe('hdr-host');

    const out3 = extractOboHeaders({});
    expect(out3.workspaceHost).toBe('env-host');
  });

  it('customInputs.user_id wins over X-Forwarded-User', () => {
    const out = extractOboHeaders({
      customInputs: { user_id: 'ci-u' },
      headers: { 'x-forwarded-user': 'hdr-u' },
    });
    expect(out.userId).toBe('ci-u');
  });
});

describe('extractOboHeaders — header shape tolerance', () => {
  it('accepts a Headers (web-standard) object case-insensitively', () => {
    const h = new Headers();
    h.set('X-Forwarded-Access-Token', 'tok-h');
    h.set('X-Forwarded-User', 'u-h');
    const out = extractOboHeaders({ headers: h });
    expect(out.userToken).toBe('tok-h');
    expect(out.userId).toBe('u-h');
  });

  it('tolerates mixed-case keys in a plain dict', () => {
    const out = extractOboHeaders({
      headers: { 'X-Forwarded-Access-Token': 'TOK' },
    });
    expect(out.userToken).toBe('TOK');
  });

  it('tolerates underscore-style keys (Express style)', () => {
    const out = extractOboHeaders({
      headers: { x_forwarded_access_token: 'underscored' } as Record<string, string>,
    });
    expect(out.userToken).toBe('underscored');
  });

  it('handles array values (IncomingHttpHeaders shape)', () => {
    const out = extractOboHeaders({
      headers: {
        'x-forwarded-access-token': ['first-tok', 'second-tok'],
      },
    });
    expect(out.userToken).toBe('first-tok');
  });

  it('treats undefined / null header values as missing', () => {
    const out = extractOboHeaders({
      headers: { 'x-forwarded-access-token': undefined },
    });
    expect(out.userToken).toBeUndefined();
  });
});

describe('extractOboHeaders — both sources combined', () => {
  it('merges customInputs (user_token) and headers (user_email) sensibly', () => {
    const out = extractOboHeaders({
      customInputs: { user_token: 'tok' },
      headers: { 'x-forwarded-email': 'alice@example.com' },
    });
    expect(out.userToken).toBe('tok');
    expect(out.userEmail).toBe('alice@example.com');
    expect(out.userId).toBeUndefined();
  });

  it('omits fields when neither source provides them', () => {
    const out = extractOboHeaders({ customInputs: { user_token: 'tok' } });
    expect(out.userId).toBeUndefined();
    expect(out.userEmail).toBeUndefined();
    expect(out.workspaceHost).toBeUndefined();
  });

  it('ignores empty-string customInputs and falls back to headers', () => {
    const out = extractOboHeaders({
      customInputs: { user_token: '' },
      headers: { 'x-forwarded-access-token': 'hdr-tok' },
    });
    expect(out.userToken).toBe('hdr-tok');
  });
});
