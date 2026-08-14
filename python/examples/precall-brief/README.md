# Pre-Call Brief

**DataAgent over 7 governed Unity Catalog functions, called per user (OBO).** Reads one company name and returns a markdown 1-2 page sales-rep pre-call brief with 7 fixed sections: Open Orders & Shipping, Open Opportunities, Recent Win/Loss, Open RMAs, Open PPRs (product-quality issues), Field Notes, and Overdue Actions. Each section is backed by a UC function that the agent invokes via SQL — data team owns the queries, UC enforces per-user grants. Reusable: a second customer is a copy of this project with a different catalog/schema and section functions.

## What it does

A field sales rep asks "brief me on [Customer X]" and receives a formatted markdown brief pulling from 7 different backend systems (ERP, CRM, field-service system, document store) via **governed Unity Catalog functions**. The agent runs as the calling user (OBO scope), so each rep sees only the data their UC grants permit. The example ships with **offline synthetic data** so you can run end-to-end with zero real-data setup; swap `sql/vw_*.sql` to real tables once your ingestion lands.

The project demonstrates:
- **DataAgent over UC functions**: agent calls `SELECT catalog.schema.fn(company)` per section
- **Config-driven sections**: add/remove sections by editing `precall.toml`
- **OBO + UC enforcement**: per-user access control via Unity Catalog grants
- **Offline-testable design**: `contract.py` + `synthetic.py` + `brief.py` produce deterministic markdown without a live workspace
- **Databricks Apps deployment**: `databricks bundle deploy` with env-var overrides for multi-tenant reuse

## Prerequisites

- Databricks workspace with a configured CLI profile
- A running SQL warehouse (serverless auto-discovered where supported)
- Permission to create schemas, tables, and scalar functions in a UC catalog (for Part 1)

## Part 1: Workspace setup (one-time)

### Step 1: Clone the project

```bash
git clone https://github.com/stuagano/apx-agent.git
cd apx-agent/python/examples/precall-brief
```

### Step 2: Generate synthetic UC data

This step creates the schema, backing tables, 7 views, and 7 scalar functions on your workspace. All data is **synthetic** — sourced from the seeded company list in `contract.COMPANIES` — so you can test without real customer data.

```bash
cd generate
python land_uc.py \
  --profile=<your-profile> \
  --catalog=main \
  --schema=precall \
  --warehouse-id=<your-warehouse-id>
```

This creates:
- Schema `main.precall`
- 7 backing source tables (`src_vw_orders`, `src_vw_opportunities`, etc.)
- 7 governed views (`vw_orders`, `vw_opportunities`, etc.)

Verify:
```bash
databricks sql query "SELECT * FROM main.precall.vw_orders LIMIT 1" --profile=<your-profile>
```

### Step 3: Create the 7 section UC functions

```bash
python create_functions.py \
  --profile=<your-profile> \
  --catalog=main \
  --schema=precall \
  --warehouse-id=<your-warehouse-id>
```

This creates scalar functions like `main.precall.open_orders_and_shipping(company STRING)` that return JSON arrays. Each function has a rich `COMMENT` that becomes the LLM-facing tool description. Verify:

```bash
databricks sql query "SELECT main.precall.open_rmas('Example Customer 01')" --profile=<your-profile>
```

### Step 4: (Optional) Regenerate the OKF knowledge bundle

The OKF bundle under `.apx/okf/` (function cards, view cards, glossary) is **already committed**, so the agent boots with grounding out of the box. Only regenerate it if you change the contract or section set:

```bash
python gen_okf.py --catalog=main --schema=precall
```

These markdown cards become part of the agent's context (grounding its tool selection) when deployed.

## Part 2: Local development

### Step 1: Install

```bash
cd ..  # Back to the precall-brief root
uv sync
```

### Step 2: Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
# Edit .env: set DATABRICKS_PROFILE, APX_CATALOG, APX_SCHEMA, APX_WAREHOUSE_ID
```

### Step 3: Run the tests

```bash
uv run pytest
```

Tests are **offline** (no live workspace needed) and verify:
- **AC-1**: View DDLs match the frozen contract in `contract.py`
- **AC-2**: Synthetic data conforms and joins cleanly on `company`
- **AC-3**: Brief renders all 7 sections deterministically with seeded values

### Step 4: Run locally

```bash
uv run apx-agent agents run
```

This serves the agent (auto-detecting `agent_server.start_server:app`) with the dev UI at **http://localhost:8000/_apx/agent**. Ask:

```
Give me a pre-call brief for Example Customer 01
```

The agent emits a status line, calls each of the 7 section functions via SQL, parses the JSON arrays, and renders a markdown brief. The warehouse may take 10-20s to warm up on first run.

## Part 3: Deploy to Databricks Apps

### Step 1: Review `databricks.yml`

The bundle config declares:
- The app name (`precall-brief`)
- Environment variables: `APX_CATALOG`, `APX_SCHEMA`, `APX_WAREHOUSE_ID`
- The start command (uvicorn)
- OBO scopes: `["sql", "serving.serving-endpoints"]`

Edit `databricks.yml` if you want to change the default schema/catalog or the LLM endpoint.

### Step 2: Deploy

```bash
uv run apx-agent agents deploy --target apps --profile=<your-profile> \
  --var warehouse_id=<your-warehouse-id> \
  --var catalog_name=main \
  --var schema_name=precall
```

`apx-agent agents deploy` runs the bundle's build rule (stages `agent.py`, `brief.py`, the SQL/functions, and the `.apx/okf` bundle into `.build/`), pins + builds the apx-agent wheel, deploys the app, and polls it to `RUNNING`.

### Step 3: Verify deployment

```bash
databricks apps get precall-brief --profile=<your-profile> -o json | \
  python3 -c "import sys, json; d=json.load(sys.stdin); print(f\"State: {d['app_status'].get('state')}\"); print(f\"URL: {d.get('url')}\")"
```

Wait for state to be `RUNNING`, then open the app URL and ask for a brief.

## Configuration

| Variable | Default | What it controls |
|----------|---------|------------------|
| `APX_CATALOG` | `main` | UC catalog where the views and functions live |
| `APX_SCHEMA` | `precall` | UC schema where the views and functions live |
| `APX_WAREHOUSE_ID` | _(empty)_ | SQL warehouse ID for executing UC functions. If unset, a serverless warehouse is auto-discovered where supported. |
| `APX_MODEL` | `databricks-claude-sonnet-4-6` | Foundation model endpoint the agent calls |

Set these via:
- `.env` file (local development)
- `databricks.yml` variables (bundle deploy)
- Direct environment variables (any context)

## Tools

The agent has access to one tool:

- **SQL** — Execute `SELECT catalog.schema.fn(company)` for each of the 7 section functions. OBO scope enforces per-user access. No explicit `uc_function_toolkit` — the agent invokes functions via SQL so it only needs the `sql` OBO scope, not `unity-catalog`.

## Project structure

```
precall-brief/
├── agent.py                 # Agent definition: persona, section map, SQL instructions
├── brief.py                 # Headless markdown brief renderer (no LLM)
├── contract.py              # Frozen contract: VIEWS, SECTIONS, COMPANIES seed
├── synthetic.py             # Offline synthetic data generator (stdlib random only)
├── precall.toml             # Section config: titles → views
├── databricks.yml           # Bundle config for Apps deployment
├── pyproject.toml           # Project dependencies
├── .env.example             # Environment template
├── .gitignore               # Git exclusions
├── sql/
│   ├── vw_orders.sql        # 7 frozen view DDLs (frozen column contract AC-1)
│   ├── vw_opportunities.sql
│   ├── ...
├── generate/
│   ├── land_uc.py           # Creates schema, backing tables, views
│   ├── create_functions.py  # Creates 7 scalar UC functions
│   ├── gen_okf.py           # Authors OKF knowledge bundle
│   └── README.md            # How to run the generators
├── agent_server/
│   └── start_server.py      # Deploy + local entry point (framework boilerplate; don't edit)
├── scripts/
│   └── quickstart.py        # Setup verification and next-steps guide
├── tests/
│   └── test_smoke.py        # Offline gates: contract, synthetic, rendering
├── .apx/
│   └── okf/                 # Shipped knowledge bundle (function cards, view cards, glossary)
└── README.md                # This file
```

## Troubleshooting

**Q: "Warehouse is cold / queries timeout"**  
A: The SQL warehouse takes 10-60s to start if idle. The agent emits a status line ("Gathering data...") while waiting. Check warehouse state with:
```bash
databricks warehouses get <warehouse-id> --profile=<your-profile>
```

**Q: "Function not found" when running agent locally**  
A: Verify the functions exist on your workspace:
```bash
databricks sql query "SELECT main.precall.open_opportunities('Example Customer 01')" --profile=<your-profile>
```
If they don't exist, run `python generate/create_functions.py` again.

**Q: "no OBO user token" / an authorization error when the agent runs**  
A: On Databricks Apps the caller's token flows to the agent only after they authorize the app's `user_api_scopes` (`sql`, `serving.serving-endpoints`, declared in `databricks.yml`). Adding or changing scopes requires re-authorization: open the app URL and approve the consent prompt (revoke the app authorization and reopen if it's cached). Unity Catalog then enforces each rep's own EXECUTE/SELECT grants on the functions.

**Q: Tests fail with "no synthetic rows"**  
A: This is expected if the synthetic data generator fails. Verify Python >=3.11 and all dependencies are installed:
```bash
uv sync
```

**Q: Deploy fails with "source_code_path not found"**  
A: The bundle deploy expects files to be staged in `.build/`. Run:
```bash
databricks bundle deploy --profile=<your-profile>
```
The artifact build rule automatically stages the files. If it fails, check `.gitignore` — don't exclude `*.py`, `sql/`, or `.apx/`.

**Q: How do I swap synthetic → real data?**  
A: Repoint each `sql/vw_*.sql` to your real ingested tables and swap `contract.COMPANIES` seed with your real customer list. The agent code, UC functions, and OKF do not change — only the view definitions and seed.
