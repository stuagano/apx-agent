# Design: declared document handling is tools, not a new agent type

**Status:** proposed for review
**Date:** 2026-09-20
**Ticket:** #756
**Decision:** do not add `DocumentAgent` or `DocumentSource`. Compile parse → extract
as one governed `[[tool.apx.tools]]` factory that runs Databricks AI Functions
through a SQL warehouse. Ground the result with the existing `vector_search`
factory. Batch ingest, classify-routing, OCR, and chart vision stay out of v1.

## The product rule

An author should declare *which volume, which warehouse, and which extraction
schema*. APX should compile that into a caller-identity SQL tool. The author
should not paste `ai_parse_document` SQL, download PDF bytes into the App, or
hand-roll a vision prompt.

That is the same compile path as `type = "uc_function"` and
`type = "vector_search"` today (`python/src/apx_agent/_tool_config.py`
`_registry()`). A new agent class would invent a second envelope for work the
tool registry already owns.

## What already exists (do not rebuild)

| Surface | What it does | Gap |
|---|---|---|
| `[[tool.apx.tools]]` + `_registry()` | Declaration → factory callable | No document factory |
| `uc_function_tool` | Run a pre-registered UC function as the caller | Author still writes the SQL |
| `sql_tool` | LLM-authored SQL against a warehouse | Wrong shape for a fixed parse/extract |
| `vector_search_tool` / `type = "vector_search"` | Ground an existing index | Does not build the index |
| OKF / `.apx/schema.json` | Ground a *table* schema in the prompt | Not a document extraction schema |
| `contract-parsing-agent` | App downloads the PDF (PyMuPDF) and calls FM JSON-schema | Bypasses `ai_parse_document` / citations |
| `eligibility-agent` `parse_documents` | App renders page 1 and calls Claude vision | Nested LLM, no UC AI-function spine |

The two examples prove the demand. They also prove the anti-pattern: document
bytes leave Unity Catalog and extraction happens in the App process.

## What the accelerator actually is

[databricks-solutions/advance-document-processing](https://github.com/databricks-solutions/advance-document-processing)
is a set of **Lakeflow / notebook recipes**, not an agent SDK. Steal the SQL
spine. Do not port the notebooks, DABs, synthetic mortgage/paystub generators,
Tesseract, or chart-cropping loop.

| Stage | Accelerator | v1 APX | Why |
|---|---|---|---|
| Parse | `ai_parse_document` on `read_files` / binaryFile | **yes** | Native, governed, no App-side PDF library |
| Extract + citations | `ai_extract` 2.1 | **yes** | Schema-bound fields + citation payload |
| Ground | existing Vector Search index | **yes, already shipped** | `type = "vector_search"` |
| Classify / route | `ai_classify` then per-type schema | later | Needs a type table and multiple schemas |
| Chunk / index | `ai_prep_search` + Delta Sync | later | Provisioning, not a turn tool |
| Auto Loader ingest | streaming DAB | later | Jobs/Lakeflow, not the agent loop |
| OCR / word boxes | Tesseract + rapidfuzz | no | Product-specific, extra runtime |
| Chart vision | crop + serving endpoint | no | Separate multimodal product |
| >500 page stitch | chunked `pageRange` | no | Recipe, not a declaration |
| Eval harness | fuzzy / nested MLflow scoring | later | Pair with `apx-agent label` after v1 exists |

Runtime floor, from the accelerator README: DBR 17.3+ / serverless 3+ for
`ai_parse_document`; DBR 18.2+ for `ai_extract` 2.1 citations. APX must fail
loud when the warehouse cannot run those functions. It must not fall back to
PyMuPDF or a vision prompt.

## Smallest declared surface

One new factory, one new config type. The LLM sees a volume path. The factory
owns the SQL.

```toml
[tool.apx.agent]
name = "contract_review"
model = "databricks-claude-sonnet-4-6"

[[tool.apx.tools]]
type = "document_extract"
warehouse_id = "$SQL_WAREHOUSE_ID"
volume = "main.contracts.raw_contracts"
schema = "schemas/contract.json"
name = "extract_contract"

[[tool.apx.tools]]
type = "vector_search"
index_name = "main.contracts.docs_index"
columns = ["doc_id", "title", "chunk", "citation"]
```

Equivalent code form, for authors who already wire tools in Python:

```python
from apx_agent import Agent, document_extract_tool, vector_search_tool

agent = Agent(tools=[
    document_extract_tool(
        warehouse_id="abc123",
        volume="main.contracts.raw_contracts",
        schema="schemas/contract.json",
    ),
    vector_search_tool("main.contracts.docs_index"),
])
```

### Compile rules

- `volume` is a Unity Catalog volume (`catalog.schema.volume`). The tool
  argument is a path *inside* that volume. Paths outside it are
  `ToolConfigError` at build or `ToolError` at call — never interpolated into
  SQL as raw text.
- `schema` is a committed JSON Schema object or a repo-relative file. It is
  the `ai_extract` schema. APX does not invent fields.
- The executed statement is owned by APX, not the model. Shape:

  ```sql
  WITH parsed AS (
    SELECT ai_parse_document(content) AS doc
    FROM READ_FILES(:volume_path, format => 'binaryFile')
  )
  SELECT ai_extract(doc, :extract_schema) AS extracted
  FROM parsed
  ```

  Bind the path and schema. Do not concatenate.
- The warehouse is required. No auto-discovery for this factory — parse cost
  and AI-function availability must be explicit.
- Identity is the calling user (OBO). The caller needs volume read + warehouse
  use. APX does not copy document bytes into the App, traces, or prompts
  beyond the SQL result the warehouse returns.
- Tool output is the `ai_extract` VARIANT plus any citation object the
  function already returns. Do not add a second provenance store. Traces
  already persist the tool payload.
- Missing warehouse, missing volume, or an AI-function error is a
  `ToolError` that names the Databricks function. No PyMuPDF / vision fallback.

### Why not a UC function the author writes?

`type = "uc_function"` already works. It is the escape hatch, not the product.
The product is: the extraction schema lives in the agent declaration, the SQL
is compiled, and two example agents can delete their App-side PDF parsers.

### Why not classify in v1?

`ai_classify` is real and useful. It needs a label set *and* a map from label
to extract schema. That is a second declaration (`types.paystub`,
`types.w2`, …) and a routing branch. One schema on one volume is enough to
retire the contract-parsing anti-pattern.

## Out of scope for this design

- `DocumentAgent`, `DocumentSource`, or a `template = { name = "document" }`
- Creating or refreshing a Vector Search index
- Auto Loader / Lakeflow job generation
- OCR, chart crops, page stitching, >500-page `pageRange`
- Copying citation bounding boxes into trace tags
- Porting the accelerator's mortgage / paystub / insurance generators
- Held-out extraction eval (use `apx-agent label` later, against gold tables)

## Follow-up implementation slice (not this note)

1. Add `document_extract_tool` next to `sql_tool` / `vector_search_tool`.
2. Register `type = "document_extract"` in `_tool_config._registry()`.
3. Unit-test declaration parse, path-escape, and "no fallback" error copy
   with a fake warehouse. No live `ai_parse_document` in CI.
4. Point `docs/tools/overview.md` at the factory. The factory shipped in
   `#796`; the two example agents now call it instead of App-side PDF parse.

Held-out FP/FN on extraction quality is a later human/eval step, same as
MemAlign (#709).
