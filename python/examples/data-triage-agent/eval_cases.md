# Data Triage Agent — Eval Cases

Use in the dev UI at `/_apx/agent` or via `POST /responses`.

## General queries (routes to general agent → sub-agent + tools)

### 1. Table schema lookup (🔗 sub-agent + ⚡ tool)
```
Show me the schema for serverless_stable_qh44kx_catalog.explain_my_bill.customers
```
**Expected:** 🔗 data_inspector fires, ⚡ get_table_info fires. Returns column names, types, row count (10 rows).

### 2. Row count check (🔗 sub-agent)
```
How many customers are in serverless_stable_qh44kx_catalog.explain_my_bill.customers?
```
**Expected:** 🔗 data_inspector returns "10 rows."

### 3. Specific record lookup (🔗 sub-agent)
```
Look up customer CUST-0003 in serverless_stable_qh44kx_catalog.explain_my_bill.customers
```
**Expected:** 🔗 data_inspector returns Priya Patel, Green Energy Plan, 88 Sunset Blvd.

### 4. Delta version history (🔗 sub-agent)
```
Show the version history for serverless_stable_qh44kx_catalog.explain_my_bill.customers
```
**Expected:** 🔗 data_inspector returns 2 versions — CREATE TABLE at v0, WRITE (10 rows) at v1, both by stuart.gano on 2026-04-03.

### 5. Lineage trace (⚡ tool)
```
What upstream sources feed into serverless_stable_qh44kx_catalog.explain_my_bill.customers?
```
**Expected:** ⚡ get_table_lineage fires. Returns notebook entity as the upstream source.

### 6. Job lookup (⚡ tool)
```
What jobs write to serverless_stable_qh44kx_catalog.explain_my_bill.billing_history?
```
**Expected:** ⚡ find_jobs_for_table fires. Returns writer entities.

---

## Investigation queries (routes to 6-step pipeline → streaming steps)

### 7. Missing customer investigation (full pipeline)
```
CUST-0011 is missing from serverless_stable_qh44kx_catalog.explain_my_bill.customers. Investigate why.
```
**Expected:** Step 1/6 through Step 6/6 stream. ⚡ run_sql_query + get_table_info fire in Step 1. Verdict: DATA MISSING — CUST-0011 never ingested (table has CUST-0001 through CUST-0010).

### 8. Pipeline failure check (investigation pipeline)
```
Why did the pipeline that writes to serverless_stable_qh44kx_catalog.explain_my_bill.billing_history fail?
```
**Expected:** Routes to investigation pipeline (hits "fail" keyword). Step 2 traces lineage, Step 3 checks job history.

### 9. Stale data check (investigation pipeline)
```
The data in serverless_stable_qh44kx_catalog.explain_my_bill.ami_hourly_rollups seems stale. When was it last updated?
```
**Expected:** Routes to investigation pipeline (hits "stale" keyword). Step 1 checks table freshness.

### 10. Data gap investigation (investigation pipeline)
```
There's a data gap in serverless_stable_qh44kx_catalog.explain_my_bill.billing_history for March 2026. Investigate.
```
**Expected:** Routes to investigation pipeline (hits "data gap"). Step 1 queries for March records.

---

## Edge cases

### 11. Greeting (no tools)
```
Hello, what can you help me with?
```
**Expected:** General agent responds with capabilities list. No tools fire.

### 12. Ambiguous query
```
Something seems wrong with the customers table
```
**Expected:** Routes to general agent (no strong investigation keyword). Asks for clarification or checks the table.

---

## Verifying routing

| Query contains | Routes to | Tool types |
|---|---|---|
| "missing", "investigate", "fail", "stale", "data gap" | Investigation pipeline (6 steps) | ⚡ local tools per step |
| Table queries without investigation keywords | General agent | 🔗 data_inspector sub-agent |
| "hello", "what can you do" | General agent | No tools |
