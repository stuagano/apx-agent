# Genie Spaces as code

Write each Genie Space as a config file, check it offline, and create, update
or promote it with `apx-agent resources genie-space`. The full reference is
[docs/deploy/genie-agents-as-code.md](../../../../docs/deploy/genie-agents-as-code.md);
this page covers the workflow.

## Creation Workflow

### Step 1: Inspect the tables

Before writing the file, look at what the tables hold: names, column types,
row counts, and how they join.

```sql
DESCRIBE TABLE EXTENDED my_catalog.sales.orders;
SELECT * FROM my_catalog.sales.orders LIMIT 10;
```

### Step 2: Plan the space

1. **Select tables** that support the use case (silver or gold, not bronze)
2. **Identify key columns**: dates, metrics, dimensions, join keys
3. **Write down the joins** between tables
4. **Plan sample questions** that use the real column vocabulary

### Step 3: Write the config file

Start from an existing space with `pull`, or write the file by hand:

```yaml
# genie/sales.yml
title: Sales Analytics
description: |
  Explore retail sales data:
  - customers: region, segment and signup date
  - orders: order_date, total_amount and status
  Tables join on customer_id.
warehouse_id: abc123def456
parent_path: /Workspace/Shared/genie        # optional; used only on create
serialized_space:
  version: 2
  config:
    sample_questions:
      - id: 01f0c0b4e815100000000000000000a1
        question: ["What were total sales last month?"]
  data_sources:
    tables:
      - identifier: my_catalog.sales.customers
      - identifier: my_catalog.sales.orders
  instructions:
    text_instructions:
      - id: 01f0c0b4e815100000000000000000b1
        content: ["Amounts are in USD."]
    join_specs:
      - id: 01f0c0b4e815100000000000000000c1
        left: {identifier: my_catalog.sales.orders, alias: orders}
        right: {identifier: my_catalog.sales.customers, alias: customers}
        sql:
          - "`orders`.`customer_id` = `customers`.`customer_id`"
          - "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--"
```

`serialized_space` is the version 2 document as structured YAML; `push` turns
it into the escaped JSON string the API takes. Its main keys:

| Key | Contents |
|-----|----------|
| `version` | Schema version (`2`) |
| `config` | `sample_questions` shown in the UI |
| `data_sources` | `tables` (and `metric_views`), each with a `catalog.schema.table` `identifier` and optional `column_configs` |
| `instructions` | `text_instructions`, `example_question_sqls`, `sql_functions`, `join_specs`, `sql_snippets` |
| `benchmarks` | Evaluation questions with one SQL answer each |

IDs are 32-character lowercase hex and must stay sorted; the as-code doc has a
generator that produces time-ordered IDs.

### Step 4: Validate, plan, push

```bash
apx-agent resources genie-space validate genie/sales.yml               # offline, no credentials
apx-agent resources genie-space plan genie/sales.yml --profile dev     # create / update <id> + diff / noop
apx-agent resources genie-space push genie/sales.yml --profile dev     # create or update
```

`push` records the space ID under `deployments` for the workspace host, so
later pushes update the same space. Commit the file, including `deployments`.

## Updating a space

Edit the file, run `plan` to see the diff against the live space, then `push`.
A push with no changes makes no API call. If someone edited the space in the
UI, `pull` it into the file first so the change is not overwritten.

## Export and clone

- **Export / backup:** `apx-agent resources genie-space pull <space-id> genie/sales.yml --profile dev`.
  This needs edit permission on the space, because it reads `serialized_space`.
- **Clone in the same workspace:** copy the file, change `title`, remove the
  copied `deployments` entry, then `push`. Otherwise `push` would update the
  original space.

## Migrating across workspaces

Push the same file at another workspace's profile. Override the warehouse,
because warehouse IDs are workspace-specific:

```bash
apx-agent resources genie-space push genie/sales.yml --profile prod --warehouse <prod-warehouse-id>
```

Each workspace's space ID is recorded under its own host. `--warehouse` is not
written back to the file. The tables named in `serialized_space` must exist in
the target metastore.

**Different catalog names per environment:** `push` does not remap catalogs.
Keep one file per environment (for example `genie/sales.dev.yml` and
`genie/sales.prod.yml`) and replace the catalog name everywhere it appears:
table identifiers, join specs, and SQL in `example_question_sqls` and
`sql_snippets`. Run `validate` and `plan` on the result before pushing.

## Permissions Required

| Operation | Required Permission |
|-----------|-------------------|
| `pull`, `plan` against an existing space | CAN EDIT on the space (to read `serialized_space`) |
| `push` that creates a space | Can create items in `parent_path` |
| `push` that updates a space | CAN EDIT on the space |

## Best Practices

- **Descriptive names**: use `customer_lifetime_value`, not `clv`
- **Comments**: `COMMENT ON TABLE sales.customers IS 'Customer master data'`
- **Join specs** for every relationship Genie should use
- **Sample questions and benchmarks** written in the data's vocabulary, then
  scored with the benchmarks before promoting

## Troubleshooting

### `validate` rejects the file

Read the rule it names (ID format, sorting, uniqueness, join spec shape). The
as-code doc's validation table lists every rule.

### `push` reports several spaces with the same title

Record the one to manage under `deployments` for that workspace host.

### `pull` returns an empty `serialized_space`

Needs CAN EDIT on the space.

### Tables not found after promotion

The tables are missing from the target metastore, or the catalog name differs
and was not replaced (see "Different catalog names per environment").

### Poor query generation

- Use descriptive column names and add table and column comments
- Add join specs, SQL examples and sample questions in the file
- Score the space with its benchmarks after each change
