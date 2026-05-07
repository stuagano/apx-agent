# Gold Table Design: `utility_account_entities`

> Produced from the Vector Search spike (May 4, 2026). This table is the single source for
> all Vector Search indexes in the entity resolution agent. It must exist and be kept current
> before a VS endpoint/index can be created.

## Problem

Databricks Vector Search requires a **single source table** and a **single source column** per
embedding. Our matching logic depends on both name and address, which are split across two
silver tables (`account_location` and `party` in `prd_silver`). We also need multiple
name-matching strategies (full name, last name, first name + email) for different edge cases.

This gold table solves both: it joins the silver tables and materializes the composite text
columns that get embedded.

---

## Schema

```sql
CREATE TABLE <catalog>.<schema>.utility_account_entities (
  -- Identity / filter columns
  account_id              STRING NOT NULL,   -- PK, from account_location
  tenant_id               STRING NOT NULL,   -- hybrid search filter (mandatory per search)
  account_location_end    DATE,              -- NULL = currently active account
  zip_code                STRING,            -- optional secondary hybrid filter

  -- Raw entity fields (returned in search results; used for SQL fallback display)
  last_name               STRING,
  first_name              STRING,
  email                   STRING,
  service_address_line1   STRING,
  account_number          STRING,

  -- Composite text columns — one per embedding permutation
  -- These are embedded by the VS index via Delta Sync
  embed_full              STRING,   -- "{first_name} {last_name} {service_address_line1}"
  embed_last_addr         STRING,   -- "{last_name} {service_address_line1}"
  embed_first_email       STRING    -- "{first_name} {email}"
)
USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');  -- required for VS Delta Sync
```

### Embedding Permutation Rationale

| Column | Query string | Match type covered |
|--------|-------------|-------------------|
| `embed_full` | `"{first} {last} {address}"` | Standard full-name match |
| `embed_last_addr` | `"{last} {address}"` | Familial match (spouse, parent — same address, shared surname) |
| `embed_first_email` | `"{first} {email}"` | Maiden name match (first name unchanged, surname different) |

Running all three searches and unioning candidates ensures no valid match category is missed
by a single query.

---

## DLT Pipeline

```python
import dlt
from pyspark.sql import functions as F

@dlt.table(
    name="utility_account_entities",
    comment="Gold table for entity resolution VS index — joins account_location + party",
    table_properties={"delta.enableChangeDataFeed": "true"},
)
def utility_account_entities():
    acct_loc = dlt.read("prd_silver.account_location")
    party = dlt.read("prd_silver.party")

    return (
        acct_loc
        .join(party, "account_id", "left")
        .select(
            # Identity / filter
            F.col("account_id"),
            F.col("tenant_id"),
            F.col("account_location_end"),
            F.col("zip_code"),
            # Raw fields
            F.col("last_name"),
            F.col("first_name"),
            F.col("email"),
            F.col("service_address_line1"),
            F.col("account_number"),
            # Embedding permutations
            F.concat_ws(" ",
                F.col("first_name"),
                F.col("last_name"),
                F.col("service_address_line1"),
            ).alias("embed_full"),
            F.concat_ws(" ",
                F.col("last_name"),
                F.col("service_address_line1"),
            ).alias("embed_last_addr"),
            F.concat_ws(" ",
                F.col("first_name"),
                F.col("email"),
            ).alias("embed_first_email"),
        )
        # Must have at least a name to be matchable
        .filter(F.col("last_name").isNotNull() | F.col("first_name").isNotNull())
    )
```

---

## Vector Search Index Configuration

Create **one index per embedding column** (Databricks VS indexes one embedding source column
per index). Three indexes total:

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    VectorIndexType,
)

ws = WorkspaceClient()
ENDPOINT = "<uplight-vs-endpoint-name>"
SOURCE_TABLE = "<catalog>.<schema>.utility_account_entities"

for col, index_suffix in [
    ("embed_full",       "full"),
    ("embed_last_addr",  "last_addr"),
    ("embed_first_email","first_email"),
]:
    ws.vector_search_indexes.create_index(
        name=f"<catalog>.<schema>.utility_account_entities_{index_suffix}_idx",
        endpoint_name=ENDPOINT,
        primary_key="account_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=SOURCE_TABLE,
            pipeline_type="TRIGGERED",          # or CONTINUOUS for near-real-time
            embedding_source_columns=[
                EmbeddingSourceColumn(
                    name=col,
                    embedding_model_endpoint_name="databricks-gte-large-en",
                )
            ],
        ),
    )
```

### Env vars the agent needs (one per index)

| Env Var | Example value |
|---------|--------------|
| `VS_INDEX_FULL` | `catalog.schema.utility_account_entities_full_idx` |
| `VS_INDEX_LAST_ADDR` | `catalog.schema.utility_account_entities_last_addr_idx` |
| `VS_INDEX_FIRST_EMAIL` | `catalog.schema.utility_account_entities_first_email_idx` |
| `VS_ENDPOINT` | `uplight-entity-resolution` |

---

## Hybrid Search Filters

Every VS query should include at minimum `tenant_id` to avoid cross-tenant leakage. Optional
additional filters:

```python
index.similarity_search(
    query_text="Jane Smith 123 Main St",
    columns=["account_id", "last_name", "first_name", "service_address_line1", "account_number"],
    filters={"tenant_id": "utility_a", "account_location_end": None},  # None = IS NULL = active
    num_results=10,
)
```

---

## Known Unknowns / Blockers

| Item | Owner | Status |
|------|-------|--------|
| Exact catalog/schema path for gold table | Uplight | Blocked — need workspace details |
| Silver table column names (`prd_silver.*`) | Uplight | Blocked — verify against actual schema |
| VS endpoint name | Uplight | Blocked — needs to be created |
| Whether `email` is reliable enough for maiden name matching | Andrew's team | Open question |
| `pipeline_type`: TRIGGERED vs CONTINUOUS | Uplight (data freshness SLA) | Decision needed |
