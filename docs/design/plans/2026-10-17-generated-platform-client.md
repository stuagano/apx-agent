# Generated Platform Client Requirements

**Date:** 2026-10-17
**Status:** Requirements approved for implementation · **Issue:** #946
**Scope:** Keep one Databricks SDK/API adapter and generate a small typed platform client from a per-agent declaration. This is a requirements document, not an implementation design.

**Decision (2026-11-20):** Python-only. #946 asked whether the TypeScript models and browser fetch client were still required after the TypeScript runtime was retired (0e0a1f1e, #873) and the hub moved into the Python `_apx` dev UI (#825). They are not. R1–R8 below are the Python-only contract. The 2026-10-17 draft required those TypeScript outputs (models, a browser fetch client, and a TypeScript verification step); that requirement is withdrawn, not rewritten as if it had never been there. The problem statement still records the original browser-client concern.

## Problem

Databricks API handling is concentrated correctly in the Python SDK/API layer, but per-agent consumers still reach around it. Agent tools, hubs, and browser clients either repeat endpoint knowledge or import large platform models even when they need only one endpoint.

A simple agent therefore carries more platform surface than its declared tools justify. The requested change is to make each agent’s platform client exactly as large as its declared Databricks usage, while preserving existing behavior and the current boundary between platform code and agent runtimes.

## Outcome

Every agent may declare the Databricks APIs it uses. From that declaration, `apx` generates a Python SDK client exposing only those operations.

The generated client is what an agent project commits. An agent with one declared operation does not get a generated method for the rest of the Databricks surface. Existing agent features continue to behave exactly as they do today. There is no generated TypeScript client.

## Requirements

### R1. Declaration

Each agent declaration may include zero or more explicit Databricks platform operations. Each operation identifies:

- a stable local name used by agent code,
- the Databricks API operation (`WorkspaceClient` `service.method`),
- the request fields the agent is permitted to supply.

The declaration must be specific. A wildcard or “all Databricks APIs” declaration does not satisfy this requirement. The declaration object is `PlatformOperation`, stored on the agent. There is no second schema.

### R2. Generation

`apx` generates the Python platform client from the declaration. Running generation twice without a declaration change produces no file changes. A declaration change updates only that agent’s generated client.

Generated files must be recognizable as generated and must not be hand-edited. Agent business logic stays out of the generated file. The 2026-10-17 draft also asked for a hand-authored facade in front of TypeScript and Python callers; the generated Python client is the call surface, and agent tools are not folded into it.

### R3. Surface discipline

A consumer that needs one operation can import that operation without a generated method for undeclared operations. Removing an operation from an agent’s declaration must make its generated method unavailable to that agent on the next generation.

The implementation must preserve the existing rule that only the Python SDK/API layer performs direct Databricks API access. The generated method calls that boundary and does not add a second client.

### R4. Behavior preservation

The following existing behavior remains unchanged:

- agent tools keep their current call path; this change does not rewrite them,
- hub-to-agent requests keep their current routes and response content,
- approval and governance gates continue to run before any governed platform action,
- authorization, identity, and audit metadata remain attached to the same actions as before,
- errors returned for invalid requests, denied actions, and platform failures retain their current meaning and user-visible content.

Generating a client must not change runtime behavior for an agent that does not call the generated client.

### R5. Client safety

The generated Python client must:

- call through the `WorkspaceClient` the caller supplies,
- embed no secrets, tokens, workspace credentials, or server-only configuration,
- contain no direct HTTP call to Databricks,
- expose only operations in the declaration.

This replaces the 2026-10-17 browser-client rule. That rule required a TypeScript fetch client that used app authentication, carried no credentials, and could not call Databricks directly from the browser. No such client is generated.

### R6. Documentation

This plan’s usage example and `generate_platform_client`’s module docstring show how to declare an operation, run generation, and call the generated method. They describe the shipped Python client, not a TypeScript migration. Generated files state that they are regenerated rather than edited.

### R7. Verification

Automated tests prove the outcome with real generation and client usage:

- an agent declaration containing one operation generates only that operation,
- the generated Python method is callable with the declared arguments, calls the existing Python SDK/API boundary, and returns its result unchanged,
- the generated source embeds no Databricks credentials and no direct Databricks HTTP client,
- regenerating without changes is idempotent,
- removing an operation removes its generated method,
- existing agent, hub, governance, and API regression tests continue to pass.

A test that only checks for a generated filename, or mocks away both the platform boundary and the returned result, does not satisfy this requirement. The 2026-10-17 draft also required a TypeScript model round-trip and a browser-client request test. Those checks are withdrawn with the TypeScript outputs.

### R8. Commit gate

Before completion, the full repository check passes, including the Python test suite and the lint suite. There is no TypeScript verification step. The generator, the declaration type, and the tests are committed. An agent project that declares operations commits the generated client next to that declaration.

## Explicitly out of scope

- Replacing the Python Databricks SDK/API layer.
- Generating every Databricks API regardless of agent declarations.
- Moving governance, identity, or authorization decisions into generated code.
- A new hub or agent feature unrelated to platform-client generation.
- Rewriting existing agent tools except where an import must change to the generated client.
- TypeScript models and a browser-safe TypeScript fetch client (withdrawn; #873).
- A second declaration format besides `PlatformOperation` on the agent.

## Acceptance scenario

Given an agent whose declaration lists one Databricks platform operation, when `apx` generates its client, then Python callers can invoke that operation on the generated client with the declared arguments, no unrelated platform operation is a method on that client, and the agent’s existing response behavior remains unchanged.

## Usage

```python
from pathlib import Path

from apx_agent import Agent, PlatformOperation, declare_platform_operations, generate_platform_client

agent = Agent(name="sales", tools=[])
declare_platform_operations(
    agent,
    [
        PlatformOperation(
            name="list_catalogs",
            operation="catalogs.list",
            fields=("include_browse",),
        )
    ],
)
path = generate_platform_client(agent, Path("generated"))
# generated/sales_platform_client.py is generated. Do not edit it.
# SalesPlatformClient(workspace).list_catalogs(include_browse=True)
# calls workspace.catalogs.list and returns that result.
```

`generated/` must be a directory; generation creates it if needed. `workspace` is a `databricks.sdk.WorkspaceClient` the caller already built. The generated method does not read credentials.

## Chosen implementation (2026-11-20)

These were open choices. This is the one that satisfies the Python-only requirements without a second platform-access boundary:

- Declaration is `PlatformOperation` (`name`, `operation`, `fields`), stored on the agent by `declare_platform_operations`. Not TOML and not YAML.
- Generation introspects `databricks.sdk.WorkspaceClient` in-process. It does not call Databricks.
- One file per agent: `<agent_name>_platform_client.py`, class `<AgentName>PlatformClient`.
- The generated method passes only the declared fields through to the SDK method and returns that call’s result.

## Open implementation choices

The choices below were intentionally not requirements of the 2026-10-17 draft. The chosen implementation above is one way to meet R1–R8. A different layout would still be acceptable if it kept a single Python SDK boundary and did not bring back a TypeScript client.

- the declaration schema and file location,
- whether generation uses an OpenAPI document, SDK introspection, or checked-in operation metadata,
- generated filenames and module layout,
- how local operation names map to SDK methods.
