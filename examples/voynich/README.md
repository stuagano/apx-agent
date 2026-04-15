# voynich-agents

Evolutionary cryptanalysis system for the Voynich manuscript, built on
[apx-agent](https://github.com/stuagano/apx-agent) and Databricks.

Treats Voynich decipherment as a program optimization problem via an
AlphaEvolve-style evolutionary loop: mutate cipher hypotheses, evaluate
them with multi-agent fitness signals, select survivors via Pareto
frontier, repeat.

## Architecture

```
voynich/
├── loop_agent/              # LoopAgent — new apx-agent workflow primitive
│   ├── __init__.py          # Public API
│   ├── loop_agent.py        # LoopAgent, Hypothesis, LoopConfig, GenerationResult
│   └── population_store.py  # Delta Lake R/W (Spark bulk + SQL fallback)
│
├── agents/
│   ├── decipherer/          # Hypothesis mutation and cipher application
│   ├── historian/           # Medieval RAG — period plausibility scoring
│   ├── critic/              # Adversarial falsifier (top-5% candidates only)
│   ├── judge/               # Agent eval — scores reasoning quality, not outputs
│   └── orchestrator/        # Loop controller + researcher interface
│
├── notebooks/
│   ├── 01_load_corpus.py    # Ingest EVA transliteration → Delta
│   ├── 02_build_indexes.py  # Build Vector Search indexes for medieval corpora
│   └── 03_review_gate.py    # Human review gate between generation batches
│
└── tests/
    └── test_loop_agent.py   # Unit tests (pure logic, mocked I/O)
```

## Agents

| Agent | Role | Databricks primitive |
|---|---|---|
| **Orchestrator** | Loop controller, researcher UI | Apps + Workflows |
| **Decipherer** | Hypothesis mutation generator | Model Serving |
| **Historian** | Medieval RAG fitness scorer | Vector Search |
| **Critic** | Adversarial falsifier | Model Serving |
| **Judge** | Agent eval (reasoning quality) | MLflow Tracing |

### Key design decision: Agent evals

The **Judge** agent evaluates *agent reasoning quality*, not hypothesis quality.
A Critic that hallucinated a contradiction gets a low Judge score.
A Critic that correctly said "I cannot falsify this" gets a high score.
This is what makes the system self-calibrating rather than just running
a fixed fitness function at scale.

### LoopAgent primitive

`LoopAgent` extends apx-agent's workflow vocabulary (`Sequential`, `Parallel`,
`Loop`, `Router`, `Handoff`) with generation-level population management:

```python
from apx_agent import create_app
from loop_agent import LoopAgent, LoopConfig

loop = LoopAgent(config=LoopConfig(
    population_table  = "voynich.evolution.population",
    fitness_agents    = [os.getenv("HISTORIAN_URL"), os.getenv("CRITIC_URL")],
    mutation_agent    = os.getenv("DECIPHERER_URL"),
    judge_agent       = os.getenv("JUDGE_URL"),
    warehouse_id      = os.getenv("DATABRICKS_WAREHOUSE_ID"),
))
app = create_app(loop)  # standard apx-agent pattern
```

## Setup

### Prerequisites

- Databricks workspace with Unity Catalog enabled
- SQL warehouse
- Vector Search endpoint
- Model Serving endpoints for each agent
- Databricks Apps enabled

### 1. Install the loop_agent package

```bash
pip install -e ".[dev]"
```

### 2. Run setup notebooks in order

```
notebooks/01_load_corpus.py      # Stage EVA file to DBFS first
notebooks/02_build_indexes.py    # Builds Vector Search indexes
```

### 3. Deploy agents as Databricks Apps

Each agent is a standalone Databricks App. Deploy with:

```bash
cd agents/decipherer
databricks apps deploy --name voynich-decipherer

cd ../historian
databricks apps deploy --name voynich-historian

# ... repeat for critic, judge, orchestrator
```

Set environment variables in each app's configuration:

```bash
# In agents/orchestrator/pyproject.toml [tool.apx.agent.env]:
DECIPHERER_AGENT_URL = "https://voynich-decipherer.<workspace>.databricksapps.com"
HISTORIAN_AGENT_URL  = "https://voynich-historian.<workspace>.databricksapps.com"
CRITIC_AGENT_URL     = "https://voynich-critic.<workspace>.databricksapps.com"
JUDGE_AGENT_URL      = "https://voynich-judge.<workspace>.databricksapps.com"
DATABRICKS_WAREHOUSE_ID = "<your-warehouse-id>"
```

### 4. Deploy the Workflow

```bash
databricks jobs create --json @SCHEMA_AND_WORKFLOW.sql
```

Or trigger a single generation batch manually:

```bash
# Via the Orchestrator App chat UI
# Open: https://voynich-orchestrator.<workspace>.databricksapps.com/_apx/agent
# Ask:  "Run 10 generations"
```

## Development

```bash
# Run tests
pytest tests/ -v

# Type check
pyright loop_agent/

# Dev server (orchestrator)
cd agents/orchestrator
uvicorn main:app --reload --port 8001
```

## Relationship to apx-agent

`LoopAgent` is designed as a PR candidate for the main `apx-agent` repo.
It follows all existing conventions:
- Tools are plain Python functions with type-hint schemas
- `Dependencies.*` injection via FastAPI
- `create_app()` compatibility
- `pyproject.toml` `[tool.apx.agent]` config pattern

The Spark bulk-write path in `PopulationStore` requires `pyspark` (available
in Workflow/notebook contexts). The SQL fallback works everywhere including
Apps. This matches the Databricks platform's existing compute model.

## Data lineage

```
EVA Corpus (voynich.corpus.*)
    │
    ├─→ Decipherer reads symbol frequencies
    │
    ├─→ Historian queries via Vector Search
    │       └─→ Medieval corpora (voynich.medieval.*)
    │
    ├─→ Critic reads illustration metadata
    │
    └─→ Population written to voynich.evolution.population
            │
            ├─→ MLflow tracks every generation
            ├─→ Review queue surfaced in Apps UI
            └─→ Agent evals logged to voynich.evolution.agent_evals
```

## License

Apache 2.0 — same as apx-agent.
