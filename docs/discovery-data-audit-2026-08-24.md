# Discover Data audit and implementation

Date: 2026-08-24  
Scope: `/_apx/discover` in the built-in development UI

## Audit result

Before this change, Discover was an agent and tool wiring surface. It could
list workspace agents, Unity Catalog functions, Model Serving endpoints, Genie
spaces, and Vector Search indexes, but it did not provide a data browsing flow.
In particular, it had no catalog/schema/table metadata path, no table schema
inspection, and no bounded row-sampling endpoint.

The existing Setup page had catalog, schema, table, and warehouse selectors,
but its table route returned names only. Reusing that route would not have
provided schema metadata or a preview, so Discover now has a focused data
contract while reusing the existing OBO-safe catalog, schema, and warehouse
inventory routes.

## Delivered behavior

The Discover page now supports:

- selecting a Unity Catalog catalog and schema;
- listing up to 100 visible tables with table type, comment, columns, types,
  nullability, and an available row-count statistic;
- selecting a table to inspect its metadata;
- running a bounded `SELECT * ... LIMIT N+1` preview with a 1–100 row cap;
- selecting a SQL warehouse or allowing the existing SQL helper to choose one;
- rendering null, numeric, boolean, string, and structured values safely in the
  preview table;
- showing empty, loading, permission, and sampling-error states.

The API contract is:

- `GET /_apx/discover/tables?catalog=<catalog>&schema=<schema>`
- `GET /_apx/discover/sample?catalog=<catalog>&schema=<schema>&table=<table>&limit=<n>`

Both routes use `_ws_prefer_obo(request)`, so the inventory and sample run as
the signed-in user in Databricks Apps. The sample route validates identifiers,
quotes each Unity Catalog identifier component, and fails closed on unsafe
input. Results are bounded and the extra fetched row is used only to report
whether more rows are available.

## Verification

- Focused route/UI tests cover OBO selection, returned schema metadata,
  identifier quoting, row bounding, truncation, and unsafe-input rejection.
- The rendered Discover script passes `node --check`.
- Existing workspace discovery, Discover wiring, Dev UI, and hot-route tests
  pass in the targeted suite.
- No live workspace data was read or changed during implementation.

## Deliberate boundary

This finishes the first useful data-discovery slice; it does not claim to be a
full profiling or semantic-discovery system. Distributions, null percentages,
PII classification, generated descriptions, and automatic SQL-tool generation
remain follow-on work. They should be added only after this bounded,
identity-scoped browsing flow is exercised against representative workspaces.
