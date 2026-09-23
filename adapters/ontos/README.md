# Governance — Ontos Adapter

A pluggable governance UI module that connects a Databricks governance scan engine
to [Ontos](https://github.com/databrickslabs/ontos) (Databricks Labs
governance platform). Think of it as the **Grafana data source plugin**
for Governance's **Prometheus-like** scan engine.

This adapter lives in the [apx-agent](https://github.com/stuagano/apx-agent)
monorepo under `adapters/ontos/` and targets **vanilla** `databrickslabs/ontos`
as the host platform — no Ontos fork or patches required, just the three-line
registration below. It is distributed from source (path or git install); it is
not published to PyPI.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Ontos (Platform)                                             │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐│
│  │  Governance Plugin Contract (REST API)                    ││
│  │  /api/governance/violations, /policies, /exceptions, ...  ││
│  └──────────────────────────────────────────────────────────┘│
│            ▲                                                  │
│            │  React views consume this API                    │
│  ┌─────────┴────────────────────────────────────────────────┐│
│  │  Frontend: 4 pages (Dashboard, ResourceDetail,           ││
│  │            Policies, Exceptions)                          ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
         ▲
         │  implements
         │
┌────────┴─────────────────────────────────────────────────────┐
│  apx-ontos-governance package (this adapter)                │
│                                                               │
│  GovernanceProvider protocol → GovernanceProvider (Delta SQL)    │
│  Thin FastAPI routers → dependency-injected provider           │
└──────────────────────────────────────────────────────────────┘
         ▲
         │  reads/writes
         │
┌────────┴─────────────────────────────────────────────────────┐
│  platform.governance.* Delta tables                             │
│  (written by the Governance Monitoring scan engine)                        │
└──────────────────────────────────────────────────────────────┘
```

**Key design principle:** The Delta tables are the integration contract.
The adapter reads them; the engine writes them. They deploy independently.

## Quick Start

### Integrate with Ontos (3 lines)

**Backend** — in Ontos `app.py`:

```python
from ontos_governance import register_routes as register_governance
register_governance(app)
```

**Frontend** — copy `frontend/src/` into Ontos fork, then:

```typescript
// config/features.ts
import { governanceFeatures } from './features.governance'
export const features = [...existingFeatures, ...governanceFeatures]

// app.tsx — inside /governance route children
import governanceRoutes from './routes.governance'
children: [...existingGovernanceChildren, ...governanceRoutes]
```

### Install (from source)

```bash
# path install from a local apx-agent checkout
pip install -e "adapters/ontos[governance,dev]"

# or straight from git
pip install "apx-ontos-governance[governance] @ git+https://github.com/stuagano/apx-agent.git#subdirectory=adapters/ontos"
```

### Standalone mode (development)

```bash
cd adapters/ontos
pip install -e ".[governance,dev]"

export DATABRICKS_HOST=https://adb-xxx.azuredatabricks.net
export DATABRICKS_WAREHOUSE_HTTP_PATH=/sql/1.0/warehouses/xxx
export DATABRICKS_TOKEN=dapi...
export GOVERNANCE_ONTOLOGY_DIR=../engine/ontologies

uvicorn ontos_governance.app:app --reload
```

## GovernanceProvider Protocol

Any governance backend can implement this protocol. The default
`GovernanceProvider` reads from Delta tables.

| Domain | Methods |
|--------|---------|
| **Violations** | `violations_summary()`, `list_violations(filters)` |
| **Scans** | `list_scans(limit)`, `get_scan(scan_id)` |
| **Resources** | `list_resources(filters)`, `get_resource(resource_id)` |
| **Policies** | `list_policies(filters)`, `get_policy(id)`, `create_policy(...)`, `update_policy(...)`, `policy_history(id)` |
| **Exceptions** | `list_exceptions(filters)`, `exceptions_summary()`, `approve_exceptions(...)`, `revoke_exception(id)`, `bulk_revoke_expired()` |
| **Ontology** | `list_ontology_classes(kind)`, `get_ontology_class(name)`, `ontology_tree()`, `validate_ontology()` |

See `provider.py` for the full Protocol definition with type signatures.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `GOVERNANCE_CATALOG` | `platform` | Unity Catalog name |
| `GOVERNANCE_SCHEMA` | `governance` | Schema within the catalog |
| `DATABRICKS_HOST` | — | Workspace URL |
| `DATABRICKS_WAREHOUSE_HTTP_PATH` | — | SQL warehouse HTTP path |
| `DATABRICKS_TOKEN` | — | PAT or OAuth token |
| `DATABRICKS_CLIENT_ID` | — | SP client ID (alternative to token) |
| `DATABRICKS_CLIENT_SECRET` | — | SP client secret (alternative to token) |
| `GOVERNANCE_ONTOLOGY_DIR` | — | Path to `resource_classes.yml` directory |

## Writing a Custom Provider

Implement the `GovernanceProvider` protocol and pass it to `register_routes`:

```python
from ontos_governance import GovernanceProvider, register_routes

class MyProvider:
    def violations_summary(self) -> ViolationSummary:
        # your implementation
        ...
    # ... implement all protocol methods

register_routes(app, provider=MyProvider())
```

Register as an entry point for auto-discovery:

```toml
[project.entry-points."ontos_governance.providers"]
my-provider = "my_package:MyProvider"
```

## Directory Structure

```
adapters/ontos/
├── pyproject.toml                    # apx-ontos-governance package
├── tests/                            # unit tests (run: pytest adapters/ontos/tests/)
├── README.md
├── src/ontos_governance/
│   ├── __init__.py                   # exports: GovernanceProvider, register_routes
│   ├── models.py                     # Pydantic models (the data contract)
│   ├── provider.py                   # GovernanceProvider protocol
│   ├── providers/
│   │   └── governance.py               # GovernanceProvider (Delta SQL)
│   ├── routers/
│   │   ├── _deps.py                  # Shared dependencies (provider, current_user)
│   │   ├── violations.py             # GET violations, scans, resources
│   │   ├── policies.py               # CRUD policies + history
│   │   ├── exceptions.py             # CRUD exceptions + bulk operations
│   │   └── ontology.py               # GET class hierarchy, tree, validate
│   ├── router.py                     # Mount all routers, register_routes()
│   ├── app.py                        # Standalone FastAPI app
│   └── ontos_sync.py                 # Semantic link sync (Governance → Ontos)
└── frontend/
    ├── README.md                     # Frontend integration guide
    └── src/
        ├── config/features.governance.ts
        ├── i18n/en/governance.json
        ├── routes.governance.tsx
        └── views/
            ├── GovernanceDashboard.tsx
            ├── ResourceDetail.tsx
            ├── Policies.tsx
            └── Exceptions.tsx
```
