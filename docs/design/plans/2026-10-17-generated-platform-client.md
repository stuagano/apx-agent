# Generated Platform Client Requirements

**Date:** 2026-10-17
**Status:** Requirements approved for implementation · **Issue:** #946
**Scope:** Keep one Databricks SDK/API adapter and generate a small typed platform client from a per-agent declaration. This is a requirements document, not an implementation design.

## Problem

Databricks API handling is concentrated correctly in the Python SDK/API layer, but per-agent consumers still reach around it. Agent tools, hubs, and browser clients either repeat endpoint knowledge or import large platform models even when they need only one endpoint.

A simple agent therefore carries more platform surface than its declared tools justify. The requested change is to make each agent’s platform client exactly as large as its declared Databricks usage, while preserving existing behavior and the current boundary between platform code and agent runtimes.

## Outcome

Every agent declares the Databricks APIs it uses. From that declaration, `apx` generates:

- a Python SDK client exposing only those operations,
- TypeScript request and response models for the same operations,
- a browser-safe TypeScript fetch client for those operations,
- a hand-authored facade that agent tools and hub code consume.

The generated artifacts are committed. An agent with one declared operation does not import a client for the rest of the Databricks surface. Existing agent features continue to behave exactly as they do today.

## Requirements

### R1. Declaration

Each agent declaration may include zero or more explicit Databricks platform operations. Each operation identifies:

- a stable local name used by agent code,
- the Databricks API operation,
- whether the operation is called from Python, TypeScript, or both,
- any request fields the agent is permitted to supply.

The declaration must be specific. A wildcard or “all Databricks APIs” declaration does not satisfy this requirement.

### R2. Generation

`apx` generates the Python and TypeScript platform artifacts from the declaration. Running generation twice without a declaration change produces no file changes. A declaration change updates only the generated client and model files required by the changed operations.

Generated files must be recognizable as generated and must not be hand-edited. The hand-authored facade and agent business logic remain separate from generated files.

### R3. Surface discipline

A consumer that needs one operation can import that operation without importing models or methods for undeclared operations. Removing an operation from an agent’s declaration must make its generated method unavailable to that agent on the next generation.

The implementation must preserve the existing rule that only the Python SDK/API layer performs direct Databricks API access. TypeScript generation must not introduce a second API adapter or direct Databricks SDK/API client.

### R4. Behavior preservation

The following existing behavior remains unchanged:

- agent tools continue to call platform operations through the hand-authored facade,
- hub-to-agent requests keep their current routes and response content,
- approval and governance gates continue to run before any governed platform action,
- authorization, identity, and audit metadata remain attached to the same actions as before,
- errors returned for invalid requests, denied actions, and platform failures retain their current meaning and user-visible content.

Generating a client must not change runtime behavior for an agent whose declaration represents only its existing platform usage.

### R5. Browser safety

The generated TypeScript browser client must:

- use the existing app authentication and request path,
- avoid secrets, tokens, workspace credentials, and server-only configuration,
- reject or be unable to express a direct call to Databricks from the browser,
- expose only operations declared for browser use.

### R6. Documentation and examples

Documentation and at least one example agent show how to declare an operation, run generation, and consume the generated method through the facade. Docs must describe the current behavior rather than a planned migration, and must state that generated files are regenerated rather than edited.

### R7. Verification

Automated tests prove the outcome with real generation and client usage:

- an agent declaration containing one operation generates only that operation,
- the generated Python method calls the existing Python SDK/API boundary and returns its result unchanged,
- the generated TypeScript model accepts a valid response and rejects a malformed one,
- the browser client sends the expected app request and does not contain Databricks credentials or a direct Databricks client,
- regenerating without changes is idempotent,
- existing agent, hub, governance, and API regression tests continue to pass.

A test that only checks for a generated filename, or mocks away both the platform boundary and response validation, does not satisfy this requirement.

### R8. Commit gate

Before completion, the full repository check passes, including the Python test suite and TypeScript verification. Generated artifacts are committed alongside the declaration, facade, tests, and docs that explain them.

## Explicitly out of scope

- Replacing the Python Databricks SDK/API layer.
- Generating every Databricks API regardless of agent declarations.
- Moving governance, identity, or authorization decisions into generated code.
- A new hub or agent feature unrelated to platform-client generation.
- Rewriting existing agent tools except where an import must change to the generated facade.

## Acceptance scenario

Given an example agent that currently uses one Databricks platform operation, when its declaration lists only that operation and `apx` generates its client, then Python and TypeScript callers can invoke that operation through the facade, no unrelated platform operation is importable from the generated client, and the agent’s existing response behavior remains unchanged.

## Open implementation choices

These choices are intentionally not requirements:

- the declaration schema and file location,
- whether generation uses an OpenAPI document, SDK introspection, or checked-in operation metadata,
- generated filenames and module layout,
- how the facade maps local operation names to SDK methods.

Any choice is acceptable if it satisfies all requirements above without adding a second platform-access boundary.
