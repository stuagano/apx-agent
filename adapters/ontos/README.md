# Ontos Governance Adapter

`apx-ontos-governance` puts governance scan results (violations, policies,
exceptions, grants, remediation proposals) inside
[Ontos](https://github.com/databrickslabs/ontos), the Databricks Labs data
governance platform, and annotates Unity Catalog assets in Ontos with the
ontology classes the scan engine assigned them.

It is a self-contained package in the apx-agent monorepo. apx-agent core does
not import it, and it is not published to PyPI — install it from source.

## Why it exists

The governance scan engine (in the companion governance-monitoring repo, not
here) finds problems: untagged tables, over-broad grants, failing data-quality
checks. Data stewards already work in Ontos. This adapter is the bridge, so
findings show up where stewards act on them instead of in a separate tool.

The only coupling between engine and UI is a set of **Delta tables**. The
engine writes them; the adapter reads them (and writes back steward decisions).
Either side can be redeployed without the other, as long as the table shapes
hold.

## How it works

```
scan engine (companion repo)
   │ writes
   ▼
<catalog>.<schema>.* Delta tables          ← the integration contract
   │ SQL via databricks-sql-connector
   ▼
GovernanceProvider   (providers/governance.py)
   │ FastAPI dependency override
   ▼
/api/governance/* routers  ──►  React views inside the Ontos frontend

ontos_sync.py:  resource_classifications ──POST──►  Ontos /api/semantic-links/
```

There are three moving parts.

### 1. The provider — reads and writes Delta

`GovernanceProvider` runs SQL on a SQL warehouse against
`<GOVERNANCE_CATALOG>.<GOVERNANCE_SCHEMA>` (default `platform.governance`).

| Table | Adapter access | Used for |
|-------|----------------|----------|
| `violations`, `scan_results` | read | violation summary/list, scan history |
| `resource_inventory` | read | resources, grants |
| `resource_classifications` | read | resource detail; source for Ontos sync |
| `dq_status` | read | per-resource data-quality metrics |
| `policies`, `policies_history` | read + write | policy list, create/edit, history |
| `exceptions` | read + write | approve / revoke / bulk-revoke-expired |
| `remediation_proposals`, `remediation_reviews` | read + write | review inbox |
| `v_remediation_funnel`, `v_agent_effectiveness`, `v_reviewer_load` | read | remediation dashboard |

Policies with `origin = 'yaml'` are owned by the engine's policy YAML in git:
the API refuses to edit them. Change those in git and redeploy the engine, or
create a user policy instead.

The ontology (class hierarchy) is **not** in Delta. It is loaded from
`resource_classes.yml` in `GOVERNANCE_ONTOLOGY_DIR` — the same file the engine
classifies with. `POST /api/governance/ontology/validate` flags derived classes
with no classifier, missing parents, and cycles.

Any backend can replace the default by implementing the `GovernanceProvider`
protocol in `provider.py` and passing it to `register_routes(app, provider=...)`.

### 2. The routers — the REST contract the UI consumes

`register_routes(app)` mounts six thin routers under `/api/governance`:
violations (plus scans and resources), policies, exceptions, grants, ontology,
and remediation, plus `GET /api/governance/health`. All logic lives in the
provider. Writes are attributed to the `x-forwarded-user` header, which the
Databricks Apps proxy sets for the signed-in user (`unknown` outside Apps).

### 3. The sync — classifications into Ontos

`ontos_sync.sync_classifications_to_ontos()` is a one-way push:

1. Take the latest `scan_id` in `resource_classifications`.
2. Keep only UC resources (`table`, `schema`, `catalog` → Ontos `uc_table`,
   `uc_schema`, `uc_catalog`). Jobs, clusters, warehouses etc. are skipped —
   Catalog Commander only shows UC assets.
3. POST one semantic link per `(resource_id, class_name)` to
   `<ontos_url>/api/semantic-links/`, with IRI `<base_iri><class_name>`.

The result is ontology-class annotations on UC assets in Ontos Catalog
Commander. Links that already exist (409, or 400 "already exists") count as
`skipped`, so re-running is safe. It only adds links: a class that a resource
lost in a later scan is **not** removed from Ontos.

## Setup

### Prerequisites

- The scan engine deployed and writing the tables above to a UC schema.
- An Ontos instance (a Databricks App) you can redeploy.
- A SQL warehouse, and a principal that can `SELECT` the governance schema
  (and `MODIFY` on `policies`, `policies_history`, `exceptions`,
  `remediation_proposals`, `remediation_reviews` for the write paths).
- A copy of the engine's `resource_classes.yml`.

### 1. Install the backend into Ontos

```bash
# from a local apx-agent checkout
pip install -e "adapters/ontos[governance]"

# or from git
pip install "apx-ontos-governance[governance] @ git+https://github.com/stuagano/apx-agent.git#subdirectory=adapters/ontos"
```

Then in Ontos `app.py`:

```python
from ontos_governance import register_routes as register_governance
register_governance(app)
```

`register_routes()` builds the provider from these environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `GOVERNANCE_CATALOG` | `platform` | Catalog holding the governance tables |
| `GOVERNANCE_SCHEMA` | `governance` | Schema holding the governance tables |
| `GOVERNANCE_ONTOLOGY_DIR` | — | Directory containing `resource_classes.yml` |
| `DATABRICKS_HOST` | — | Workspace URL |
| `DATABRICKS_WAREHOUSE_HTTP_PATH` | — | SQL warehouse HTTP path |
| `DATABRICKS_TOKEN` | — | Token for the warehouse connection |
| `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` | — | SP credentials (used when no token is set) |

The provider connects lazily, so the app starts even when these are wrong —
misconfiguration shows up as 500s on the first request. Check
`/api/governance/violations/summary` after deploying, not just `/health`.

### 2. Add the frontend views

The views use Ontos's own UI primitives (`@/components/ui/*`, `@/hooks/use-api`,
`@/hooks/use-toast`, `@/stores/breadcrumb-store`), so they must be built
inside the Ontos frontend. See [`frontend/README.md`](frontend/README.md) for
the copy step and the feature/route/i18n registration lines.

The views mount under Ontos's `/governance` route; the dashboard lands at
`/governance/violations`.

### 3. Schedule the Ontos sync

Nothing in this repo calls the sync — run it after each engine scan, typically
as a task after the engine's evaluate step in the same Databricks job:

```python
from ontos_governance.ontos_sync import sync_classifications_to_ontos

result = sync_classifications_to_ontos(
    spark,
    catalog="platform",
    schema="governance",
    ontos_url="https://<ontos-app>.databricksapps.com",
    secret_scope="governance",   # holds ontos_client_id / ontos_client_secret
    dry_run=False,
)
# {'posted': N, 'skipped': N, 'errors': N, 'total_candidates': N}
```

It needs `pyspark` (present on Databricks compute), `databricks-sdk`, and
`requests` — these are **not** declared in this package's dependencies, so
install them alongside it when running anywhere else.

**Auth.** The Databricks Apps proxy in front of Ontos accepts only OAuth JWTs
(`eyJ...`); PATs (`dapi...`) and the serverless job's internal token are
rejected. The token is resolved in this order:

1. `ontos_token=` argument
2. `DATABRICKS_TOKEN`, only if it is a JWT
3. SDK SP credentials (`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`) → M2M token
4. Secret scope `ontos_client_id` + `ontos_client_secret` → M2M token
   (**use this for scheduled jobs** — a fresh token every run, nothing expires)
5. Secret scope `ontos_token` — a stored JWT that expires in about an hour; dev only

The resolved principal needs `CAN_USE` on the Ontos app and `semantic-models`
write access inside Ontos (Ontos admins have it by default).

**IRI base.** Links point at `<base_iri><class_name>`, where `base_iri` is
`ontology_base_iri=`, else `GOVERNANCE_ONTOLOGY_BASE_IRI`, else
`<workspace_host>/ontology/governance/class/`. Keep it stable — changing it
creates a second set of links instead of updating the first.

Run with `dry_run=True` first; it logs every link it would post and needs no
token.

## Keeping it in sync

There are three kinds of drift to manage.

| What | Changes when | What to do |
|------|--------------|------------|
| **Classification data** | every engine scan | Run the sync after each scan (above). Watch `errors` in its result — a failed POST is logged and counted, not raised. Stale links for de-classified resources must be removed in Ontos by hand. |
| **Table contract** | the engine adds/renames columns or tables | Update `models.py` and the SQL in `providers/governance.py` in the same change window as the engine. The adapter's tests stub the warehouse, so they will not catch a live schema change — hit the deployed endpoints after an engine upgrade. |
| **Adapter code in Ontos** | this adapter changes, or Ontos upgrades | Backend: reinstall the package (pin a git SHA, not a branch) and redeploy Ontos. Frontend: re-copy `frontend/src/*` — the copies in Ontos do not update themselves — and rebuild. After an Ontos upgrade, confirm the UI primitives the views import still exist. |
| **Ontology file** | the engine's `resource_classes.yml` changes | Ship the same file to `GOVERNANCE_ONTOLOGY_DIR`, then `POST /api/governance/ontology/validate`. A mismatch shows the wrong hierarchy in the UI and links IRIs Ontos doesn't know. |

## Development

```bash
# standalone API against a real workspace
cd adapters/ontos
pip install -e ".[governance,dev]"
export DATABRICKS_HOST=... DATABRICKS_WAREHOUSE_HTTP_PATH=... DATABRICKS_TOKEN=...
export GOVERNANCE_ONTOLOGY_DIR=/path/to/dir/with/resource_classes.yml
uvicorn ontos_governance.app:app --reload   # docs at /docs

# tests (from the repo's python/ env; conftest stubs databricks.sql / sdk / pyspark)
cd python && uv run pytest ../adapters/ontos/tests -q
```

CI runs these in their own step (`Run adapter tests` in `.github/workflows/ci.yml`)
on every code change.

## Layout

```
adapters/ontos/
├── pyproject.toml                 # apx-ontos-governance
├── src/ontos_governance/
│   ├── __init__.py                # exports GovernanceProvider, register_routes
│   ├── models.py                  # Pydantic models — the API data contract
│   ├── provider.py                # GovernanceProvider protocol
│   ├── providers/governance.py    # default provider (Delta SQL)
│   ├── routers/                   # violations, policies, exceptions, grants,
│   │                              # ontology, remediation, _deps
│   ├── router.py                  # register_routes()
│   ├── app.py                     # standalone FastAPI app
│   └── ontos_sync.py              # classifications → Ontos semantic links
├── frontend/src/                  # views, routes, features, i18n to copy into Ontos
└── tests/
```
