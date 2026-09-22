# PRD: Declared external-model endpoint seam (`model = "bedrock:…"`)

**Slug:** declared-external-model
**Status:** ready for implementation
**Scope:** provision + reconcile a governed Databricks external-model serving endpoint from a declared provider-scheme + `[tool.apx.agent.gateway]` sub-table.

## Problem

apx compiles a UC-grounded, governed runtime from a declaration today, but the
**model** is assumed to be a serving endpoint that already exists. To use a
non-native model (route token spend to an AWS/Azure commit, escape the native
Foundation Model API catalog/pricing), an author must hand-create a Databricks
**external-model** serving endpoint plus its Mosaic AI Gateway config
(guardrails, usage tracking, `CAN_QUERY`) out-of-band, then reference it by
name. That is exactly the "wired, not declared" gap apx exists to close.

Everything to close it already exists as primitives:
`ResourceSpec("serving_endpoint", model)` is already added in
`collect_resource_specs` (`python/src/apx_agent/_resources.py:352-353`) and
already projects to a `CAN_QUERY` `databricks.yml` entry
(`_resources.py:479-486`); the config parser already walks nested
`[tool.apx.agent.*]` sub-tables (`_inspection.py:_load_agent_config` @207-298,
`AgentConfig` nested-config pattern in `_models.py:395-414`); the doctor already
reads `ep.ai_gateway.guardrails` back off an endpoint
(`_doctor.py:check_gateway_guardrails` @515-583). This assembles them plus a
**scheme parser + data-driven provider registry** and a **provision/reconcile**
step on the deploy path.

## Solution

1. **Provider-scheme parse via a data-driven registry.** `model = "bedrock:anthropic.claude-3-5-sonnet"`
   (also `azure:`, `anthropic:`, any Databricks external-model provider) parses
   `<scheme>:<model_name>` into an external-model endpoint spec. A provider is a
   **registry entry (data)**, not a code path: a table mapping scheme →
   `{databricks external-model provider name, credential env/field name(s),
   endpoint-name template}`. Adding a provider = adding a row. A bare `model`
   with no known scheme keeps today's behavior (treated as an existing endpoint
   name — no external-model provisioning). An unknown scheme fails clear at
   parse time.

2. **`[tool.apx.agent.gateway]` inline sub-table, governance-ON defaults.** A
   new `GatewayConfig` (pydantic, in `_models.py`) added as an `AgentConfig`
   field, parsed automatically by the existing nested walk. Defaults:
   usage-tracking **on**, PII/safety guardrails **on**, `credential` **required**
   (no invented default — a missing credential is a deploy error, not a silent
   fallback). `credential` is a UC service credential name; a `secret_scope` /
   `secret_key` pair is the documented fallback when no UC service credential
   exists.

3. **Provision + full reconcile on deploy (create + update, never delete).**
   On deploy, when `model` carries a known scheme, apx provisions the
   external-model serving endpoint via the **REST API**
   (`POST /api/2.0/serving-endpoints`) — the CLI `--json` path strips
   `uc_service_credential_name` (memory `project_bedrock_via_ai_gateway`), so
   the REST/SDK path that carries the field is required. `bedrock_provider` must
   be lowercase; `name` lives inside the JSON body. Reconcile: **get** the
   endpoint; **create** it if absent; **update** its config (model, provider,
   `ai_gateway`, credential) if present so redeploy converges to the
   declaration. **Never delete** an endpoint in v1 (out of scope).

4. **Fail-closed.** If the declared credential is missing/unreachable, or the
   endpoint create/reconcile call fails, the deploy **fails closed** (raises /
   refuses) — aligned with the user's prior watchdog fail-closed default. No
   partial "deployed but ungoverned" state.

5. **Reuse the CAN_QUERY resource + doctor reader.** The declared external-model
   endpoint's name flows through `collect_resource_specs` →
   `resources_to_databricks_yml` and emits the existing `CAN_QUERY`
   `serving_endpoint` entry (`_resources.py:479-486`) — no new resource kind.
   The reality-ctk gate reads the emitted/created spec back using the same
   attribute reads the doctor uses (`ep.ai_gateway`, guardrails).

### Integration points (exact)

- **New module** `python/src/apx_agent/_external_model.py`: `parse_model_scheme(model) -> ExternalModelSpec | None`,
  the `_PROVIDER_REGISTRY` table, `build_endpoint_payload(spec, gateway, credential) -> dict`
  (the `POST /api/2.0/serving-endpoints` body incl. `external_model` +
  `ai_gateway`), and `reconcile_external_model_endpoint(ws, payload) -> None`
  (get→create-or-update, never delete; fail-closed on error).
- **`_models.py`** (~@395-414): add `class GatewayConfig` + `gateway` field on
  `AgentConfig` (governance-ON defaults; `credential` required for external
  schemes).
- **`_inspection.py:_load_agent_config`** (@207-298): no change — nested walk
  already parses `[tool.apx.agent.gateway]` once the field exists.
- **`foundation_model.py`** (@84-108) / model resolution: when a scheme is
  present, the resolved *endpoint name* (from the registry's name template) is
  what gets passed downstream and attached as `ResourceSpec("serving_endpoint", <name>)`.
- **Deploy path** `cli.py deploy()` (@6631+, apps branch ~@7225, service
  resources emit @9516-9519): call `reconcile_external_model_endpoint` before /
  as part of resource provisioning; ensure the endpoint name is in the spec set
  so `CAN_QUERY` is emitted.
- **`_doctor.py:check_gateway_guardrails`** (@515-583): reused as-is by the
  reality gate (reads `ep.ai_gateway.guardrails`); no change required.

## Non-goals

- **Price-map / cost attribution for external tokens** — explicit follow-on PRD.
- **Deleting / tearing down endpoints** — never delete in v1 (reconcile is
  create + update only).
- **Per-user OBO on the model call** — architecturally impossible; the endpoint
  uses a shared credential.
- No new dependency (Ponytail — Databricks SDK + REST already available).
- No new `ResourceSpec` kind (reuse `serving_endpoint` / `CAN_QUERY`).

## Acceptance Criteria

- [ ] **AC-1** — Scheme parse.
  **Given** `model = "bedrock:anthropic.claude-3-5-sonnet"`,
  **When** `parse_model_scheme(model)` runs,
  **Then** it returns an `ExternalModelSpec` with `provider == "amazon-bedrock"`
  (registry-resolved) and `model_name == "anthropic.claude-3-5-sonnet"`.
- [ ] **AC-2** — Bare model unchanged.
  **Given** `model = "databricks-claude-sonnet-4-6"` (no scheme),
  **When** `parse_model_scheme(model)` runs,
  **Then** it returns `None` (existing-endpoint behavior; no provisioning).
- [ ] **AC-3** — Unknown scheme fails clear.
  **Given** `model = "quantum:foo"` (scheme not in the registry),
  **When** parsing runs,
  **Then** it raises a clear error naming the unknown scheme and the known
  schemes, and never builds a payload.
- [ ] **AC-4** — Provider registry is data, not code.
  **Given** a new provider row added to `_PROVIDER_REGISTRY` (dict entry only,
  no new function/branch),
  **When** `parse_model_scheme("<newscheme>:m")` runs,
  **Then** it resolves via that row — proving a provider is added by data. Test
  adds a row at runtime (monkeypatch) and asserts resolution with no code path
  specific to the new scheme.
- [ ] **AC-5** — `[tool.apx.agent.gateway]` parsed with governance-ON defaults.
  **Given** a pyproject with `[tool.apx.agent.gateway]` setting only
  `credential = "my_cred"`,
  **When** `_load_agent_config()` reads it,
  **Then** `config.gateway` has `usage_tracking is True`, guardrails enabled by
  default, and `credential == "my_cred"`.
- [ ] **AC-6** — Missing credential fails closed.
  **Given** a known external scheme but no `credential` and no
  `secret_scope`/`secret_key`,
  **When** the endpoint payload is built for deploy,
  **Then** it raises a clear fail-closed error and no create/update call is made.
- [ ] **AC-7** — Endpoint payload carries `external_model` + `ai_gateway`.
  **Given** a parsed `bedrock:` spec + a `GatewayConfig` with defaults + a
  credential,
  **When** `build_endpoint_payload(...)` runs,
  **Then** the returned body contains `external_model.provider` (lowercase),
  the model name, `ai_gateway` with a guardrails block and usage-tracking
  enabled, and the credential field (`uc_service_credential_name` or the
  secrets pair) — `name` inside the JSON body.
- [ ] **AC-8** — Reconcile creates when absent.
  **Given** `ws.serving_endpoints.get(name)` raises not-found,
  **When** `reconcile_external_model_endpoint(ws, payload)` runs,
  **Then** it calls the create path (`POST /api/2.0/serving-endpoints`) once and
  never calls delete.
- [ ] **AC-9** — Reconcile updates when present (never deletes).
  **Given** `ws.serving_endpoints.get(name)` returns an existing endpoint,
  **When** reconcile runs,
  **Then** it calls the update-config path (converge to declared) and never
  calls a delete path.
- [ ] **AC-10** — Reconcile fails closed on unreachable credential/endpoint.
  **Given** the create/update call raises (auth / credential / network),
  **When** reconcile runs,
  **Then** it re-raises (deploy fails closed) — no swallow, no partial success.
- [ ] **AC-11** — `CAN_QUERY` resource emitted for the external-model endpoint.
  **Given** an agent whose declared external-model endpoint name is in the
  resource set,
  **When** `resources_to_databricks_yml(specs)` runs,
  **Then** the output contains a `serving_endpoint` entry with
  `permission == "CAN_QUERY"` and `endpoint_name` equal to the declared name
  (reusing `_resources.py:479-486`).
- [ ] **AC-12** — Reality-ctk read-back of the emitted spec.
  **Given** a built endpoint payload for a `bedrock:` model + its emitted
  `databricks.yml` resources,
  **When** the reality gate reads the payload/YAML back with `ctk.verify`,
  **Then** `external_model.provider` is present and lowercase, an `ai_gateway`
  block with guardrails is present, and a `CAN_QUERY` `serving_endpoint`
  resource is present (non-empty, must-contain — not `.exists()`).
- [ ] **AC-13** — Live Bedrock end-to-end (manual / deferred).
  **Given** a Bedrock-capable UC service credential (fevm lacks one — memory
  `project_bedrock_via_ai_gateway`),
  **When** an author declares `model = "bedrock:…"` + gateway credential and
  deploys,
  **Then** the endpoint is created, guardrails + usage tracking are live, and
  the agent answers through it. **Not machine-verifiable in this repo/workspace**
  — manual runbook only (`verifiable: false`).

## Convergence

- **stopping_signal:** `cd python && uv run pytest -k external_model -q` green
  AND `make check` green.
- **progress_metric:** failing gate-test count (target 0), AC-1..AC-12.
- **editability:** high — one new module (`_external_model.py`), one
  `GatewayConfig` in `_models.py`, one reconcile call on the deploy path.
- **verifiability:** high for AC-1..AC-12 (parse / registry / payload / reconcile
  branches / YAML emit / read-back are all pure or mock-driven).
- **known_ceiling:** AC-13 (live Bedrock) needs a Bedrock-capable UC credential
  fevm does not have; it is manual/deferred. Unit + spec + reality-of-emitted-spec
  gates are the loop's automatable target.

## Constraints

- **tech_stack:** Python; Databricks SDK / REST (`POST /api/2.0/serving-endpoints`);
  pydantic config; pytest + ctk.
- **key_files:**
  - `python/src/apx_agent/_external_model.py` — NEW: scheme parser,
    `_PROVIDER_REGISTRY`, `build_endpoint_payload`,
    `reconcile_external_model_endpoint`.
  - `python/src/apx_agent/_models.py` — `GatewayConfig` + `AgentConfig.gateway`
    (nested-config pattern @395-414).
  - `python/src/apx_agent/_resources.py` — reuse
    `collect_resource_specs` @352-353 + `serving_endpoint`→`CAN_QUERY` @479-486
    (no new kind).
  - `python/src/apx_agent/_inspection.py` — `_load_agent_config` @207-298
    (nested walk; no change).
  - `python/src/apx_agent/foundation_model.py` — model→endpoint resolution
    @84-108.
  - `python/src/apx_agent/cli.py` — `deploy()` @6631+, apps branch ~@7225,
    service-resource emit @9516-9519 (reconcile hook).
  - `python/src/apx_agent/_doctor.py` — `check_gateway_guardrails` @515-583
    (reused by reality gate; no change).
  - `python/tests/test_external_model.py` — NEW gate tests (AC-1..AC-11).
  - `python/tests/test_external_model_reality_ctk.py` — NEW reality read-back
    (AC-12).
- **patterns:** reuse `ResourceSpec`/`resources_to_databricks_yml` and the
  existing model→endpoint resolution and the doctor gateway reader (Ponytail);
  ctk read-after-write on the emitted/created spec (Ctk); provider = data row,
  not code path; fail-closed on missing/unreachable credential.
- **lint:** no `.get(k, "")`, no `x or ""`, no invented env defaults, no
  `object` annotations, no `tuple[...]` returns, no skipped tests.
- **CLI gotcha:** `serving-endpoints create --json` strips
  `uc_service_credential_name` — use REST/SDK path that carries it;
  `bedrock_provider` lowercase; `name` inside the JSON body.

## Agent Handoff

```json
{
  "goal": "Let an apx-agent declare its model with a provider scheme (bedrock:/azure:/anthropic:/…) resolved by a data-driven provider registry, and on deploy provision + fully reconcile (create+update, never delete) a governed Databricks external-model serving endpoint with Mosaic AI Gateway (guardrails + usage tracking ON by default), a UC service credential (secrets fallback), and a CAN_QUERY resource — failing the deploy closed if the credential/endpoint is unreachable.",
  "tech_stack": "Python; Databricks SDK/REST (POST /api/2.0/serving-endpoints); pydantic config; pytest + ctk",
  "constraints": {
    "key_files": [
      "python/src/apx_agent/_external_model.py",
      "python/src/apx_agent/_models.py",
      "python/src/apx_agent/_resources.py",
      "python/src/apx_agent/_inspection.py",
      "python/src/apx_agent/foundation_model.py",
      "python/src/apx_agent/cli.py",
      "python/src/apx_agent/_doctor.py",
      "python/tests/test_external_model.py",
      "python/tests/test_external_model_reality_ctk.py"
    ],
    "no_new_deps": true,
    "never_delete_endpoints": true,
    "cli_gotcha": "serving-endpoints create --json strips uc_service_credential_name; use REST/SDK; bedrock_provider lowercase; name inside JSON body"
  },
  "patterns": [
    "reuse ResourceSpec('serving_endpoint', name) + resources_to_databricks_yml → existing CAN_QUERY entry (_resources.py:479-486); no new resource kind",
    "add GatewayConfig to _models.py; _load_agent_config nested walk parses [tool.apx.agent.gateway] with no parser change",
    "provider is a registry row (data), not a code path — adding a provider = adding a dict entry",
    "reconcile = get → create-if-absent / update-if-present; never delete (v1)",
    "fail closed on missing/unreachable credential or endpoint call failure (no swallow)",
    "reuse _doctor.check_gateway_guardrails read pattern (ep.ai_gateway.guardrails) in the reality gate",
    "ctk read-after-write on emitted payload/YAML: external_model.provider + ai_gateway + CAN_QUERY present"
  ],
  "convergence": {
    "stopping_signal": "cd python && uv run pytest -k external_model -q  (green) AND make check (green)",
    "progress_metric": "failing gate-test count across AC-1..AC-12 (target 0)",
    "known_ceiling": "AC-13 live Bedrock needs a Bedrock-capable UC credential fevm lacks (memory project_bedrock_via_ai_gateway) — manual/deferred"
  },
  "escalate_on": [
    "Databricks external-model serving payload shape (external_model / ai_gateway / uc_service_credential_name fields) differs from assumed and no REST/SDK path carries the credential",
    "the reconcile update path cannot converge an existing endpoint's provider/credential without a delete (which is out of scope)",
    "GatewayConfig governance-ON defaults conflict with an existing AgentConfig validation rule",
    "resources_to_databricks_yml cannot carry the external-model endpoint name through to a CAN_QUERY entry without a new resource kind"
  ],
  "acceptance_criteria": [
    {
      "id": "AC-1",
      "description": "parse_model_scheme('bedrock:anthropic.claude-3-5-sonnet') returns ExternalModelSpec(provider='amazon-bedrock', model_name='anthropic.claude-3-5-sonnet') via the registry.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_parse_bedrock_scheme"
    },
    {
      "id": "AC-2",
      "description": "parse_model_scheme('databricks-claude-sonnet-4-6') (no scheme) returns None — existing endpoint behavior, no provisioning.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_bare_model_returns_none"
    },
    {
      "id": "AC-3",
      "description": "Unknown scheme raises a clear error naming the unknown + known schemes and builds no payload.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_unknown_scheme_fails_clear"
    },
    {
      "id": "AC-4",
      "description": "A provider added as a _PROVIDER_REGISTRY row (data only) resolves through parse_model_scheme with no scheme-specific code path.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_provider_registry_is_data"
    },
    {
      "id": "AC-5",
      "description": "[tool.apx.agent.gateway] with only credential set parses to gateway.usage_tracking=True, guardrails enabled by default, credential preserved.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_gateway_config_governance_on_defaults"
    },
    {
      "id": "AC-6",
      "description": "Known external scheme with no credential and no secret_scope/secret_key raises a fail-closed error; no create/update call is made.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_missing_credential_fails_closed"
    },
    {
      "id": "AC-7",
      "description": "build_endpoint_payload returns a body with external_model.provider (lowercase), model name, ai_gateway (guardrails + usage tracking), credential field, and name inside the JSON.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_payload_has_external_model_and_gateway"
    },
    {
      "id": "AC-8",
      "description": "reconcile_external_model_endpoint calls create once when get() raises not-found and never calls delete.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_reconcile_creates_when_absent",
      "mock": "ws.serving_endpoints.get raises not-found; create + delete spied"
    },
    {
      "id": "AC-9",
      "description": "reconcile updates config when the endpoint exists and never calls delete.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_reconcile_updates_when_present",
      "mock": "ws.serving_endpoints.get returns existing; update + delete spied"
    },
    {
      "id": "AC-10",
      "description": "reconcile re-raises when the create/update call fails (deploy fails closed) — no swallow, no partial success.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_reconcile_fails_closed_on_error",
      "mock": "create/update side_effect=raise; assert propagated"
    },
    {
      "id": "AC-11",
      "description": "resources_to_databricks_yml emits a serving_endpoint entry with permission CAN_QUERY and endpoint_name equal to the declared external-model endpoint name.",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model.py",
      "gate_test": "test_can_query_resource_emitted"
    },
    {
      "id": "AC-12",
      "description": "Reality read-back: the built payload + emitted databricks.yml carry external_model.provider (lowercase), an ai_gateway guardrails block, and a CAN_QUERY serving_endpoint resource — verified non-empty + must-contain via ctk.verify, not .exists().",
      "test_type": "pytest",
      "verifiable": true,
      "gate_file": "python/tests/test_external_model_reality_ctk.py",
      "gate_test": "test_emitted_external_model_spec_is_real"
    },
    {
      "id": "AC-13",
      "description": "Live Bedrock end-to-end: declared bedrock: model deploys, endpoint created with live guardrails + usage tracking, agent answers through it. Manual runbook only — fevm lacks a Bedrock-capable UC credential.",
      "test_type": "manual",
      "verifiable": false,
      "gate_file": "",
      "gate_test": ""
    }
  ]
}
```
