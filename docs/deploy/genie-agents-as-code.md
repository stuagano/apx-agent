# Genie Agents as code

Declare a Genie Agent (the Management API calls it a *space*) in a file in your
repo, validate it offline, and create, update or promote it with the
[Genie Management API](https://docs.databricks.com/aws/en/genie-agents/conversation-api).

```bash
apx-agent resources genie-space pull <space-id> genie/sales.yml --profile dev   # export
apx-agent resources genie-space validate genie/sales.yml                         # offline
apx-agent resources genie-space plan genie/sales.yml --profile dev               # diff
apx-agent resources genie-space push genie/sales.yml --profile dev --dry-run     # plan only
apx-agent resources genie-space push genie/sales.yml --profile dev               # create/update
```

## The config file

YAML by default; a `.json` path reads and writes JSON with the same keys.
`serialized_space` is the version 2 document, written as structured YAML
rather than the escaped JSON string the API takes. `push` escapes it.

```yaml
title: Sales analytics
description: Ask questions about orders and customers.
warehouse_id: 1234567890abcdef
parent_path: /Workspace/Shared/genie     # optional; used only on create
deployments:                             # written by pull/push, one entry per workspace
  https://dev.cloud.databricks.com: 01f0c0b4e8151000000000000000000a
serialized_space:
  version: 2
  data_sources:
    tables:
      - identifier: sales.analytics.customers
      - identifier: sales.analytics.orders
  instructions:
    text_instructions:
      - id: 01f0c0b4e815100000000000000000b1
        content: ["Amounts are in USD."]
    join_specs:
      - id: 01f0c0b4e815100000000000000000c1
        left: {identifier: sales.analytics.orders, alias: orders}
        right: {identifier: sales.analytics.customers, alias: customers}
        sql:
          - "`orders`.`customer_id` = `customers`.`customer_id`"
          - "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--"
```

Unknown top-level keys are rejected so a typo cannot be silently ignored.
Quote an ID that is all digits (YAML would otherwise read it as a number).
`pull` rewrites the whole file; `push` only adds a `deployments` entry and keeps
the rest of the file, including YAML comments.

## Validation

`validate`, `plan` and `push` all check the documented `serialized_space`
rules before touching a workspace. `validate` needs no workspace or credentials.

| Rule | Checked |
|---|---|
| Version | `version` must be `2`. |
| ID format | IDs are 32-character lowercase hex without hyphens, in `config.sample_questions`, `benchmarks.questions`, and every `instructions` collection (`text_instructions`, `example_question_sqls`, `sql_functions`, `join_specs`, `sql_snippets.filters` / `expressions` / `measures`). |
| Sorting | `data_sources.tables` and `metric_views` by `identifier`; each `column_configs` by `column_name`; ID collections by `id`; `sql_functions` by `(id, identifier)`. |
| Uniqueness | IDs are unique across sample questions plus benchmark questions, and across all instruction collections; each `(table, column_name)` is configured once. |
| Limits | Every string at most 25,000 characters; every list at most 10,000 items; at most 1 text instruction. |
| Names | Table, metric view and join identifiers use the three-level `catalog.schema.table` name; MCP connections need `connection_name`; each `data_sources.volumes` entry needs a unique `/Volumes/<catalog>/<schema>/<volume>` `path`, at most 10 volumes. |
| Join specs | `sql` is exactly two strings: a condition referencing both aliases backtick-quoted (`` `orders`.`customer_id` ``) and a `--rt=FROM_RELATIONSHIP_TYPE_{MANY_TO_ONE,ONE_TO_MANY,ONE_TO_ONE,MANY_TO_MANY}--` annotation. |
| Snippets / benchmarks | Snippet `sql` is not empty; each benchmark has exactly one answer with `format: SQL`. |

Workspace-specific limits (table counts, SQL length) are not documented as
numbers and are left to the API.

To make a new ID, use the generator from the Databricks docs (time-ordered, so
appended items stay sorted):

```bash
python3 -c "import random,datetime;t=int((datetime.datetime.now()-datetime.datetime(1582,10,15)).total_seconds()*1e7);print(f'{(t&0xFFFFFFFFFFFF0000)|(1<<12)|((t&0xFFFF)>>4):016x}{random.getrandbits(62)|0x8000000000000000:016x}')"
```

## Attached UC volumes

Volumes that a Genie Agent reads files from (Public Preview, **Analyze Files in
Volumes with Genie Agents**) can be attached in the agent's **Sources** tab or
declared in `serialized_space` under `data_sources.volumes`. This is the shape
`get-space --include-serialized-space` returns and `create-space` accepts
(checked live on 25 Nov 2026):

```yaml
serialized_space:
  data_sources:
    volumes:
      - path: /Volumes/main/docs/handbooks
        description:
          - Project handbook (PDF). Cite the file and page.
```

`validate` checks that each `path` is a `/Volumes/<catalog>/<schema>/<volume>`
path, declared once, that `description` (optional) is a non-empty list of
strings, and that there are at most 10 volumes. `push` sends `serialized_space`
as a full replacement, so a declared volume is attached on create and update.
Removing a volume from the file should detach it on the next push; that has
not been checked against a live workspace. Content search then indexes the
volume. That takes several minutes (about 5 for a 3-page PDF), and questions
asked before it finishes fail with `volume_index_not_ready`.

To declare the volumes as App resources, also pass them to the tool:
`genie_agent_tool(space_id, volumes=["catalog.schema.volume", ...])`. See
[Genie Agent mode](../tools/overview.md#files-in-uc-volumes-public-preview).

## How push finds the space

1. The ID recorded under `deployments` for the target workspace host, if that
   space still exists.
2. Otherwise a space with the same `title`. Two or more matches is an error:
   record the one to manage under `deployments`.
3. Otherwise `push` creates the space and records its ID.

`plan` prints `create`, `update <id>` with a unified diff (live → local) of
`title`, `description`, `warehouse_id` and `serialized_space`, or `noop`.
`push` with no changes makes no API call. `push` never deletes or trashes a
space; to retire one, trash it in the workspace.

## Promotion

Push the same file at another workspace's profile, overriding the warehouse:

```bash
apx-agent resources genie-space push genie/sales.yml --profile prod --warehouse <prod-warehouse-id>
```

The prod space ID is recorded under its own host, so dev and prod each update
their own space on later pushes. `--warehouse` is not written back to the file.
Tables named in `serialized_space` must exist in the target workspace's
metastore.

Exporting with `pull` needs permission to read the space's `serialized_space`
(edit permission on the space).
