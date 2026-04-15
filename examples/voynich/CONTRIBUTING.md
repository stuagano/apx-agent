# Contributing to voynich-agents

This guide covers the three most common contribution patterns:
adding a tool to an existing agent, adding a new agent, and writing tests.

## Architecture in one paragraph

Each agent is a Databricks App built on `apx-agent`. Tools are plain Python
functions whose type hints become the JSON schema and whose docstring becomes
the tool description. `Dependencies.Workspace` / `Dependencies.Sql` parameters
are injected by FastAPI and hidden from the LLM. The `LoopAgent` in
`loop_agent/` orchestrates generations by dispatching to agent `/invocations`
endpoints, reading/writing Delta Lake via `PopulationStore`, and logging
everything to MLflow.

---

## 1. Adding a tool to an existing agent

Tools live in `agents/<name>/main.py`. A tool is any function that:
- Takes typed parameters (Pydantic-annotated or plain Python types)
- Takes `Dependencies.*` parameters for Databricks access (injected, not in schema)
- Returns a `dict` (serialized to JSON in the tool response)
- Has a docstring (becomes the tool's description for the LLM)

```python
# agents/historian/main.py

def score_linguistic_register(
    decoded_text: Annotated[str, "Decoded candidate text to analyze"],
    expected_register: Annotated[str, "Expected register: formal_latin | vernacular | liturgical"],
    ws: Dependencies.Workspace = None,
) -> dict:
    """
    Score whether decoded text matches the expected linguistic register
    for a 15th-century manuscript section.
    """
    # ... implementation ...
    return {"register_score": 0.72, "detected_register": "formal_latin", ...}
```

Then register it in the agent:

```python
agent = Agent(
    tools=[
        ...,
        score_linguistic_register,   # add here
    ],
    instructions="...",
)
```

**Write a test** in `tests/agents/test_agent_tools.py`:

```python
class TestHistorianTools:
    def test_linguistic_register_formal_latin(self):
        r = self.m.score_linguistic_register(
            "Radix huius plantae in aqua cocta ventris dolorem sedat.",
            "formal_latin",
        )
        assert r["register_score"] > 0.5
        assert r["detected_register"] == "formal_latin"
```

---

## 2. Adding a new agent

### Step 1: Create the directory structure

```bash
mkdir -p agents/my_new_agent
```

### Step 2: Write `agents/my_new_agent/main.py`

```python
"""
My New Agent — description of what it does.
"""
from typing import Annotated
from apx_agent import Agent, Dependencies, create_app


def my_tool(
    param: Annotated[str, "Description of this parameter"],
    ws: Dependencies.Workspace = None,
) -> dict:
    """Tool docstring — shown to the LLM as the tool description."""
    return {"result": "..."}


agent = Agent(
    tools=[my_tool],
    instructions="""
    System prompt for the LLM powering this agent.
    Be specific about: when to use each tool, what to return, edge cases.
    """,
)

app = create_app(agent)
```

### Step 3: Write `agents/my_new_agent/pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "voynich-my-new-agent"
version = "0.1.0"
description = "My new agent description"
requires-python = ">=3.11"
dependencies = [
    "apx-agent>=0.16.0",
    "databricks-sdk>=0.74.0",
    "fastapi>=0.119.0",
    "pydantic>=2.0",
]

[tool.apx.agent]
name        = "voynich_my_new_agent"
description = "What this agent does (shown in A2A discovery card)"
model       = "databricks-claude-sonnet-4-6"
url         = "$MY_NEW_AGENT_URL"
registry    = "$VOYNICH_AGENT_HUB_URL"
```

### Step 4: Wire it into the orchestrator (if needed)

If the new agent participates in the evolutionary loop:
1. Add its URL to `agents/orchestrator/main.py` env vars
2. Add it to the `LoopConfig.fitness_agents` list in `loop_agent/loop_agent.py`
3. Add it to the Workflow YAML's `spark_env_vars` section

### Step 5: Write tests

```python
class TestMyNewAgentTools:
    @pytest.fixture(autouse=True)
    def _m(self): self.m = _load("my_new_agent")

    def test_my_tool_basic(self):
        r = self.m.my_tool("test input")
        assert "result" in r
```

---

## 3. Modifying LoopAgent

`LoopAgent` in `loop_agent/loop_agent.py` is the core orchestration primitive.

### Adding a new convergence condition

`_run_generation()` calls `store.get_best_fitness_history()` and checks
`max(history) - min(history) < 0.001`. To add domain-specific convergence
(e.g., "stop if adversarial fitness plateaus regardless of composite"):

```python
# In loop_agent.py _run_generation():
adversarial_history = store.get_adversarial_history(self.config.convergence_patience)
converged = (
    len(history) >= self.config.convergence_patience
    and max(history) - min(history) < 0.001
) or (
    len(adversarial_history) >= 20
    and max(adversarial_history) > 0.90   # adversarial plateau at high score
)
```

### Adding a new selection strategy

`pareto_frontier()` in `population_store.py` is a pure function — easy to
replace or augment. To add diversity preservation (crowding distance):

```python
# population_store.py
def crowding_distance_selection(
    frontier: list[Hypothesis],
    objectives: list[str],
    target_n: int,
) -> list[Hypothesis]:
    """NSGA-II crowding distance selection from frontier."""
    # ... implementation ...
```

### Adding a new tool to LoopAgent's conversational surface

`LoopAgent.tools()` returns the functions exposed to researchers via the
Orchestrator's Apps chat UI. Add any function that returns a `dict`:

```python
# loop_agent.py
def get_cipher_type_distribution(self) -> dict:
    """Get distribution of cipher types across current Pareto frontier."""
    if not self._results:
        return {"error": "no results yet"}
    survivors = self._results[-1].survivors
    from collections import Counter
    dist = Counter(h.cipher_type for h in survivors)
    return {"distribution": dict(dist), "dominant": dist.most_common(1)[0][0]}

# Then in tools():
def tools(self):
    return [
        ...,
        self.get_cipher_type_distribution,  # add here
    ]
```

---

## 4. Testing conventions

### What to test

- **Pure functions** (pareto selection, cipher application, anachronism checks):
  test exhaustively. These have no I/O and are fast.
- **Tool functions** with `Dependencies.*` params: pass `sql=None` or `ws=None`
  to exercise the no-I/O paths, mock SQL responses for read paths.
- **LoopAgent control tools** (get_status, pause, force_escalate): test directly
  since they're pure state operations.
- **Do not test** LLM routing decisions, Databricks API responses, or network calls
  in unit tests. Those belong in integration tests run against a real workspace.

### Fixture usage

```python
# conftest.py provides:
loop_config    # LoopConfig with test defaults
hypothesis     # single Hypothesis with realistic values
population     # list of 10 Hypotheses spanning cipher types
mock_ws        # MagicMock WorkspaceClient with empty SQL wired
mock_store     # PopulationStore wired to mock_ws, Spark disabled

# Use them:
def test_something(self, hypothesis, mock_store):
    mock_store.write_hypotheses([hypothesis])
    ...
```

### Running tests

```bash
make test           # all 54 tests
make test-fast      # just loop_agent tests (fastest)
make test-agents    # just agent tool tests
make coverage       # with coverage report
```

---

## 5. Pull request checklist

- [ ] `make test` passes (54/54 green)
- [ ] `make lint` passes (ruff, no new violations)
- [ ] New tools have docstrings
- [ ] New tools have at least one test per logical branch
- [ ] `pyproject.toml` updated if new dependencies added
- [ ] README updated if public API changed
- [ ] `loop_agent/__init__.py` updated if new public symbols added

## 6. Relationship to apx-agent upstream

`loop_agent/` is intended as a PR to `stuagano/apx-agent`. Conventions:
- Follow the `apx-agent` tool pattern exactly (typed params, `Dependencies.*`)
- Use `create_app()` for all agents
- All config via `[tool.apx.agent]` in `pyproject.toml`, never hardcoded
- `Dependencies.Workspace` for SDK calls, `Dependencies.Sql` for warehouse queries

When opening the upstream PR, the `loop_agent/` directory maps to
`src/apx_agent/workflow/loop_agent.py` in the apx-agent package tree,
with `PopulationStore` and `pareto_frontier` as sibling modules.
