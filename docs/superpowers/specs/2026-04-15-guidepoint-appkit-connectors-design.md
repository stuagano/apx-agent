# Guidepoint AppKit Connectors — Design Spec

**Date:** 2026-04-15
**Status:** Draft
**Author:** Stuart Gano

## Overview

Build domain connectors for the apx-agent AppKit TypeScript scaffold to cover Guidepoint's top use cases from their April 2026 QBR. The system comprises four independent deployable units — three AppKit agent Apps and one Databricks Workflow orchestrator — connected via A2A discovery and a shared data layer (Lakebase + Vector Search).

### Use Cases Covered

| # | Use Case | Slide Status | Deployable Unit |
|---|----------|-------------|-----------------|
| 1 | Agentic Knowledge Graph / Agentic PM | In Progress (U4, Yellow) | KG Agent App |
| 9 | More Agents (inc. Doc Upload) | Scoping | Doc Agent App |
| 11 | MCP Marketplace | Not Started | KG + Doc Apps (MCP exposure) |
| 12 | Automatic PII Flagging | Not Started (U1) | PII Agent App (watchdog) |

### Out of Scope

- SSRS Reports Migration (#2) — data engineering, not agent
- Databricks-native MDM (#3) — Lakefusion dependency, not agent
- Self-service Analytics (#4) — already works via built-in `genieSpaceMcpUrl()` helper
- Customer Data Platform (#5) — Hightouch POC, separate vendor
- Lakebase Adoption (#6, #7) — infrastructure migration, not agent
- Inference Payload Ingestion (#8) — Zerobus evaluation, blocked
- Reporting through AI/BI Dashboards (#10) — PBI Profiler exercise not started

## System Architecture

```
+------------------------------------------------------------------+
|                    Databricks Workflows                           |
|              (Orchestrator - evolutionary loop)                    |
|                                                                   |
|  Job: kg_evolution_run                                            |
|  +---------------+  +----------------+  +------------------+      |
|  | spawn_gen     |->| evaluate_      |->| select_mutate    |->loop|
|  | (call Apps)   |  | fitness        |  | (update schema)  |      |
|  +---------------+  +----------------+  +------------------+      |
+----------+--------------+--------------+-------------------------+
           |/invocations  |/invocations  |reads/writes
     +-----v------+ +-----v------+ +----v---------+
     |  KG Agent  | |  Doc Agent | | schema.yaml  | <- "genome"
     |  (AppKit)  |<--A2A-->(AppKit)| | in UC Volume |
     +-----+------+ +-----+------+ +--------------+
           |              |
     +-----v--------------v------+
     |     Shared Data Layer     |
     |  Lakebase: entities,      |
     |    edges, weights         |
     |  Vector Search: embeddings|
     +---------------------------+

     +------------+
     |  PII Agent |  (watchdog - independent)
     |  (AppKit)  |  Crawls workspace, enforces
     +------------+  policies, notifies owners
```

### Key Architectural Decisions

1. **Separate Apps per use case (not a monolith).** Each App is independently deployable, has its own A2A card, and can be consumed via MCP by Genie, Claude Desktop, or other agents.

2. **Evolutionary loop lives above the agent layer.** apx-agent handles per-request routing. The population-across-generations logic is a Databricks Workflow — each generation is a job that calls AppKit Apps via `/invocations`. The orchestrator is a long-running Workflow task, not a single agent request.

3. **Graph structure and agent behavior co-evolve.** The Workflow evaluates end-to-end match quality as the fitness function, mutating both edge weights (graph structure) and extraction/retrieval parameters (agent behavior).

4. **schema.yaml is the genome.** Stored in a UC Volume, read by all components, mutated by the Workflow. Version history via Volume snapshots.

5. **PII Agent is fully independent.** No shared data layer with KG/Doc. Separate deployment, separate concern. Connected to the ecosystem only via A2A discoverability.

6. **Data backend: Lakebase + Vector Search.** Lakebase for structured graph (entities, edges, weights). Vector Search for semantic similarity (embeddings with server-side embedding model). GP already has active Lakebase instances.

7. **Doc Upload feeds both VS and the graph.** Documents are parsed, chunked, and embedded into Vector Search for retrieval AND entity-extracted into Lakebase for graph enrichment. The Doc App and KG App share the same tables and index.

## Generic Connectors (appkit-agent)

Three new modules in `appkit-agent/ts/src/connectors/`. Each exports `defineTool()` factories following the existing pattern. Connectors are generic and reusable — Guidepoint-specific entity types come from schema.yaml, not hardcoded.

### Lakebase Connector (`connectors/lakebase.ts`)

Tool factories for structured data operations against Lakebase tables.

**Tool factories:**

```typescript
createLakebaseQueryTool(config: ConnectorConfig): AgentTool
// SELECT with parameterized queries. Accepts { table, filters, columns, limit }.
// Builds parameterized SQL internally — no raw SQL exposure.
// Returns JSON rows.

createLakebaseMutateTool(config: ConnectorConfig): AgentTool
// INSERT/UPDATE/DELETE with schema validation.
// Validates column types against schema.yaml entity definitions before executing.
// Accepts { table, operation, values, filters }.

createLakebaseSchemaInspectTool(config?: Partial<ConnectorConfig>): AgentTool
// List tables, columns, and types in a catalog.schema.
// Uses information_schema queries. Read-only.
```

**Implementation details:**
- Uses Databricks SQL Statement Execution API (`/api/2.0/sql/statements/`) via fetch
- OBO token flows through from AppKit request headers
- Parameterized queries prevent SQL injection
- Mutation tool validates against schema.yaml entity field definitions before executing
- Async poll for statement completion (statements API is async)

**Dependencies:** None beyond `fetch` (built-in). No native database drivers.

### Vector Search Connector (`connectors/vector-search.ts`)

Tool factories for semantic search and vector operations.

**Tool factories:**

```typescript
createVSQueryTool(config: ConnectorConfig): AgentTool
// Similarity search against a VS index.
// Accepts { query_text, filters, num_results }.
// Filters map to Lakebase column predicates on the indexed table.
// Returns ranked results with scores.

createVSUpsertTool(config: ConnectorConfig): AgentTool
// Add/update vectors with metadata.
// Accepts { id, text, metadata }.
// Embedding happens server-side (VS index configured with embedding endpoint).
// Metadata fields validated against schema.yaml entity definitions.

createVSDeleteTool(config: ConnectorConfig): AgentTool
// Remove vectors by filter.
// Accepts { filters } — same filter format as query.
```

**Implementation details:**
- Uses Vector Search REST API (`/api/2.0/vector-search/indexes/{index_name}/query`)
- Server-side embedding — tool sends text, VS returns ranked results
- OBO token auth
- Index name from `config.vectorSearchIndex`

### Doc Parser Connector (`connectors/doc-parser.ts`)

Tool factories for document ingestion pipeline.

**Tool factories:**

```typescript
createDocUploadTool(config: ConnectorConfig): AgentTool
// Accept file via multipart upload, store in UC Volume.
// Returns { doc_id, path, size, mime_type }.
// Uses Files API (/api/2.0/fs/files/) — streams to Volume, no memory buffering.

createDocChunkTool(config: ConnectorConfig): AgentTool
// Split document into chunks with configurable strategy.
// Accepts { doc_id } or { text }.
// Strategy from schema.yaml: chunk_size, chunk_overlap.
// Returns { chunk_id, text, position }[].

createDocExtractEntitiesTool(config: ConnectorConfig): AgentTool
// LLM-based entity extraction from chunks.
// Accepts { chunks: { chunk_id, text }[] }.
// Prompt template from schema.yaml extraction.prompt_template.
// Entity types from schema.yaml entities[].name.
// Calls FMAPI (same model as agent, or configurable).
// Returns typed entities ready for Lakebase insert + VS upsert.
```

**Implementation details:**
- Upload uses Express `multer` middleware for multipart form parsing
- Streams file content to Volume via Files API — no in-memory buffering for large docs
- Chunking strategies: fixed-size with overlap (default), or heading-based for structured docs (split on `#`, `##`, etc.)
- Entity extraction prompt is template-interpolated from schema.yaml — `{entity_names}`, `{entity_fields}`, `{chunk_text}` are replaced at runtime

### Shared Config Interface

```typescript
interface ConnectorConfig {
  /** Databricks workspace host. Defaults to DATABRICKS_HOST env var. */
  host?: string;
  /** Lakebase catalog for entity/edge tables. */
  catalog: string;
  /** Lakebase schema within the catalog. */
  schema: string;
  /** Vector Search index name. */
  vectorSearchIndex?: string;
  /** UC Volume path for document storage. */
  volumePath?: string;
  /** Schema definition loaded from schema.yaml. */
  entitySchema?: EntitySchema;
}

interface EntitySchema {
  version: number;
  generation: number;
  entities: EntityDef[];
  edges: EdgeDef[];
  extraction: ExtractionConfig;
  fitness: FitnessConfig;
  evolution: EvolutionConfig;
}

interface EntityDef {
  name: string;
  table: string;
  fields: FieldDef[];
  embedding_source?: string;
}

interface EdgeDef {
  name: string;
  table: string;
  from: string;
  to: string;
  fields: FieldDef[];
}

interface FieldDef {
  name: string;
  type: string;
  key?: boolean;
  nullable?: boolean;
  default?: number | string;
  index?: boolean;
}

interface ExtractionConfig {
  prompt_template: string;
  chunk_size: number;
  chunk_overlap: number;
}

interface FitnessConfig {
  metric: string;
  evaluation: string;
  targets: Record<string, number>;
}

interface EvolutionConfig {
  population_size: number;
  mutation_rate: number;
  mutation_fields: string[];
  selection: string;
  max_generations: number;
}
```

### Module Exports

New exports added to `appkit-agent/ts/src/index.ts`:

```typescript
// Connectors — domain tool factories
export {
  createLakebaseQueryTool,
  createLakebaseMutateTool,
  createLakebaseSchemaInspectTool,
} from './connectors/lakebase.js';

export {
  createVSQueryTool,
  createVSUpsertTool,
  createVSDeleteTool,
} from './connectors/vector-search.js';

export {
  createDocUploadTool,
  createDocChunkTool,
  createDocExtractEntitiesTool,
} from './connectors/doc-parser.js';

export type { ConnectorConfig, EntitySchema } from './connectors/types.js';
```

## Schema Config — The Genome

`schema.yaml` lives in a UC Volume at `{volumePath}/schema.yaml`. All four components read it; only the Workflow orchestrator writes it.

```yaml
version: 1
generation: 0  # incremented by Workflow each evolution cycle

# --- Entity types ---
entities:
  - name: Expert
    table: experts
    fields:
      - { name: expert_id, type: string, key: true }
      - { name: name, type: string }
      - { name: domains, type: "array<string>" }
      - { name: years_experience, type: int }
      - { name: bio_embedding, type: vector, index: true }
    embedding_source: bio

  - name: Project
    table: projects
    fields:
      - { name: project_id, type: string, key: true }
      - { name: title, type: string }
      - { name: industry, type: string }
      - { name: description_embedding, type: vector, index: true }
    embedding_source: description

  - name: Industry
    table: industries
    fields:
      - { name: industry_id, type: string, key: true }
      - { name: name, type: string }
      - { name: parent_id, type: string, nullable: true }

# --- Edge types ---
edges:
  - name: matched_to
    table: edges_matched
    from: Expert
    to: Project
    fields:
      - { name: weight, type: float, default: 0.5 }
      - { name: match_type, type: string }
      - { name: confidence, type: float }

  - name: has_domain
    table: edges_domain
    from: Expert
    to: Industry
    fields:
      - { name: strength, type: float, default: 0.5 }

  - name: belongs_to
    table: edges_project_industry
    from: Project
    to: Industry
    fields:
      - { name: relevance, type: float, default: 0.5 }

# --- Extraction rules ---
extraction:
  prompt_template: |
    Extract entities from this text. Return JSON.
    Entity types: {entity_names}
    For each entity, extract: {entity_fields}
    Text: {chunk_text}
  chunk_size: 1000
  chunk_overlap: 200

# --- Fitness function ---
fitness:
  metric: match_quality_score
  evaluation: |
    For each expert-project match, score 0-1 based on:
    - domain overlap (weight: {edges.matched_to.weight})
    - experience relevance
    - historical match success rate
  targets:
    precision_at_5: 0.8
    time_to_match_seconds: 30

# --- Evolution parameters ---
evolution:
  population_size: 5
  mutation_rate: 0.2
  mutation_fields:
    - edges.matched_to.weight
    - edges.has_domain.strength
    - edges.belongs_to.relevance
    - extraction.chunk_size
    - extraction.chunk_overlap
  selection: top_2
  max_generations: 20
```

**What the Workflow mutates:** Edge weights (`matched_to.weight`, `has_domain.strength`, `belongs_to.relevance`), chunk sizes, and extraction parameters. It does NOT mutate entity types, table names, or field definitions — those are structural.

**What each component reads:**
- **KG App:** entities, edges, fitness — to query/mutate the graph and report match quality
- **Doc App:** entities, extraction — to know what to extract from uploaded docs
- **Workflow:** everything — to evaluate fitness, mutate parameters, write new versions
- **PII App:** nothing — independent

## App Designs

### KG Agent App

**Deployment:** Databricks App (AppKit, TypeScript)
**Purpose:** Query the knowledge graph, mutate edges/weights, evaluate match quality. Called by users (via chat) and by the Workflow (via `/invocations`).

**Tools:**

| Tool | Description | Connectors Used |
|------|-------------|-----------------|
| `query_experts` | Find experts matching a project/query via VS similarity + graph traversal | `createVSQueryTool` + `createLakebaseQueryTool` |
| `query_projects` | Find projects matching an expert profile | `createVSQueryTool` + `createLakebaseQueryTool` |
| `get_match_explanation` | Explain why an expert matched a project (edge weights, domain overlap) | `createLakebaseQueryTool` |
| `mutate_edge` | Update edge weight or create new edge between entities | `createLakebaseMutateTool` |
| `eval_fitness` | Score a set of matches against schema.yaml fitness targets | `createLakebaseQueryTool` + custom scoring logic |
| `get_schema` | Return current schema.yaml version and generation | Volume read via fetch |

**Routing:** `RouterAgent` with deterministic conditions:
- Messages containing expert/people names -> `query_experts` path
- Messages about projects/engagements -> `query_projects` path
- Messages from Workflow (`x-caller: workflow` header) -> `eval_fitness` path
- Fallback -> general Q&A with graph context

**A2A card:** Exports all tools via MCP. The Doc App can trigger re-indexing, and the Workflow can call fitness evaluation.

**App structure:**

```
guidepoint-kg-agent/
├── app.ts              # AppKit entry point
├── schema-loader.ts    # Read schema.yaml from Volume, parse, cache
├── tools/
│   ├── query-experts.ts
│   ├── query-projects.ts
│   ├── match-explanation.ts
│   ├── mutate-edge.ts
│   ├── eval-fitness.ts
│   └── get-schema.ts
├── router.ts           # RouterAgent config with conditions
├── databricks.yml      # DABs deployment
├── package.json
└── schema.yaml         # Local copy for dev (Volume copy is production)
```

### Doc Agent App

**Deployment:** Databricks App (AppKit, TypeScript)
**Purpose:** Accept document uploads, chunk, extract entities, and feed into the shared data layer.

**Tools:**

| Tool | Description | Connectors Used |
|------|-------------|-----------------|
| `upload_document` | Accept file upload, store in Volume, return doc_id | `createDocUploadTool` |
| `parse_and_chunk` | Split document into chunks per schema.yaml settings | `createDocChunkTool` |
| `extract_entities` | LLM-based extraction using schema.yaml entity types | `createDocExtractEntitiesTool` |
| `ingest_to_graph` | Write extracted entities to Lakebase, embeddings to VS | `createLakebaseMutateTool` + `createVSUpsertTool` |
| `get_doc_status` | Check processing status for a doc_id | `createLakebaseQueryTool` |

**Routing:** `SequentialAgent` for the happy path:

```
upload_document -> parse_and_chunk -> extract_entities -> ingest_to_graph
```

Individual tools also callable directly for re-processing (e.g., when schema evolves and entities need re-extraction).

**A2A:** KG App can call `extract_entities` and `ingest_to_graph` when the schema evolves.

**App structure:**

```
guidepoint-doc-agent/
├── app.ts              # AppKit entry point
├── schema-loader.ts    # Shared with KG App (same schema.yaml)
├── tools/
│   ├── upload-document.ts
│   ├── parse-and-chunk.ts
│   ├── extract-entities.ts
│   ├── ingest-to-graph.ts
│   └── get-doc-status.ts
├── pipeline.ts         # SequentialAgent config
├── databricks.yml
├── package.json
└── schema.yaml
```

### PII Agent App (watchdog wrapper)

**Deployment:** Databricks App (AppKit, TypeScript)
**Purpose:** Wrap the existing watchdog Python engine in an AppKit agent shell for A2A discoverability and MCP exposure.

**Tools:**

| Tool | Description | Implementation |
|------|-------------|----------------|
| `scan_workspace` | Trigger a crawl of workspace resources | Calls watchdog job via Jobs API (`/api/2.1/jobs/run-now`) |
| `evaluate_policies` | Run policy engine against resource inventory | Calls watchdog job |
| `get_violations` | Query current open violations | SQL query against watchdog Delta tables |
| `flag_pii` | Flag a specific resource as containing PII | UC Tags API + watchdog violation insert |
| `get_scan_status` | Check latest scan results and timing | SQL query against watchdog Delta tables |

**Architecture:** Thin TypeScript shell that calls the existing Python watchdog tasks via Databricks Jobs API. The heavy lifting stays in Python/PySpark. The AppKit layer provides:
- A2A discoverability (other agents can ask "does this table have PII?")
- MCP exposure (Genie spaces or users can query violations via natural language)
- Chat UI via the dev plugin

**No shared data with KG/Doc.** Watchdog maintains its own `resource_inventory`, `scan_results`, `violations` tables.

**PII detection gap:** The current watchdog policy engine checks for PII tag presence (POL-02) but does not detect PII. To close the gap for Guidepoint:
1. Add a PII column detector to the Python crawler (regex patterns for SSN, email, credit card, phone)
2. Create a `PiiTable` ontology class in watchdog
3. Deploy POL-02 with `dry_run: false`
4. The `flag_pii` AppKit tool calls the UC Tags API to apply tags, then inserts a violation record

**App structure:**

```
guidepoint-pii-agent/
├── app.ts              # AppKit entry point
├── tools/
│   ├── scan-workspace.ts
│   ├── evaluate-policies.ts
│   ├── get-violations.ts
│   ├── flag-pii.ts
│   └── get-scan-status.ts
├── watchdog-bridge.ts  # Jobs API + SQL query helpers
├── databricks.yml
└── package.json
```

### Workflow Orchestrator

**Deployment:** Databricks Job (Python, DABs)
**Purpose:** Drive the evolutionary loop — spawn generations, evaluate fitness, select and mutate schema parameters.

**Not an AppKit App.** It's a Python Databricks Workflow with three sequential tasks:

**Task 1: `spawn_generation`**
- Read current `schema.yaml` from Volume
- Create N mutated variants (per `evolution.population_size`)
- For each variant:
  - Write variant schema to a temp Volume path
  - Call KG App's `/invocations` with a test query set and the variant schema
  - Collect match results

**Task 2: `evaluate_fitness`**
- Score each variant's match results against `fitness.targets`
- Rank variants by `fitness.metric`
- Log metrics to MLflow for tracking across generations

**Task 3: `select_and_mutate`**
- Keep top performers (per `evolution.selection`)
- Breed new variants by combining weights from winners
- Apply random mutations (per `evolution.mutation_rate`) to `evolution.mutation_fields`
- Write winning `schema.yaml` back to Volume
- Increment `generation` counter

**Schedule:** Nightly, or triggered manually. Uses DABs deployment (`databricks.yml`).

**Structure:**

```
guidepoint-kg-orchestrator/
├── tasks/
│   ├── spawn_generation.py
│   ├── evaluate_fitness.py
│   └── select_and_mutate.py
├── schema_utils.py      # Load/save/mutate schema.yaml
├── fitness.py           # Scoring logic
├── databricks.yml       # DABs job definition
└── pyproject.toml
```

## Connector Effort Estimates

| Connector | Files | Estimated Size | Dependencies |
|-----------|-------|----------------|-------------|
| `connectors/lakebase.ts` | 1 | ~200 lines | fetch (built-in) |
| `connectors/vector-search.ts` | 1 | ~150 lines | fetch (built-in) |
| `connectors/doc-parser.ts` | 1 | ~250 lines | multer (new dep) |
| `connectors/types.ts` | 1 | ~80 lines | zod |
| `connectors/index.ts` | 1 | ~20 lines | re-exports |
| Tests | 3 | ~400 lines | vitest (existing) |
| **Total connector work** | **7** | **~1,100 lines** | |

## Build Sequence

### Phase 1: Connectors (Week 1-2)

1. `connectors/types.ts` — shared types and schema loader
2. `connectors/lakebase.ts` — query and mutate tools + tests
3. `connectors/vector-search.ts` — query, upsert, delete tools + tests
4. `connectors/doc-parser.ts` — upload, chunk, extract tools + tests
5. `connectors/index.ts` — re-exports
6. Update `src/index.ts` with new exports

### Phase 2: KG Agent App (Week 2-3)

1. Scaffold app with `databricks.yml`
2. Implement `schema-loader.ts` (read from Volume, parse, cache)
3. Build tools using generic connectors + schema-driven config
4. Wire `RouterAgent` with deterministic conditions
5. A2A card + MCP exposure
6. Deploy to GP workspace, test with sample data

### Phase 3: Doc Agent App (Week 3-4)

1. Scaffold app
2. Build tools using generic connectors
3. Wire `SequentialAgent` pipeline
4. A2A registration (KG App can discover it)
5. Deploy, test with sample doc upload end-to-end

### Phase 4: PII Agent App (Week 4-5)

1. Scaffold AppKit wrapper
2. Build watchdog bridge (Jobs API calls)
3. Add PII detection to Python crawler (regex patterns)
4. Create `PiiTable` ontology class, enable POL-02
5. Build `flag_pii` tool with UC Tags API
6. Deploy, test with workspace scan

### Phase 5: Workflow Orchestrator (Week 5-6)

1. Scaffold DABs job
2. Implement `schema_utils.py` (load/save/mutate)
3. Implement three tasks (spawn, evaluate, select)
4. MLflow metric logging
5. Deploy, run first generation manually
6. Validate fitness scores improve across generations

### Phase 6: Integration (Week 6)

1. End-to-end test: upload doc -> extract entities -> query graph -> evaluate fitness
2. A2A cross-app communication (KG triggers Doc re-extraction)
3. Workflow orchestrator calls both Apps
4. PII scan covers shared data layer tables
5. Demo to GP team

## Testing Strategy

**Unit tests (per connector):**
- Mock Databricks APIs (SQL Statements, Vector Search, Files)
- Validate parameterized SQL generation
- Validate schema.yaml parsing and field validation
- Validate chunk splitting logic

**Integration tests (per App):**
- Deploy to dev workspace
- Call `/responses` with test queries
- Verify tool execution and data flow

**Evolutionary loop validation:**
- Seed schema.yaml with known-bad weights
- Run 3-5 generations
- Verify fitness scores monotonically improve
- Verify schema.yaml mutation fields change

**PII detection validation:**
- Create test table with known PII columns
- Run scan + evaluate
- Verify violations surfaced and tags applied
