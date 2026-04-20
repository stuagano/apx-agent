"""
apx_agent.workflow — evolutionary loop primitives for apx-agent.

LoopAgent extends the Sequential/Parallel/Loop/Router/Handoff workflow
vocabulary with generation-level population management over Delta Lake.

Quick start:
    from apx_agent import create_app
    from apx_agent.workflow import LoopAgent, LoopConfig

    loop = LoopAgent(config=LoopConfig(
        population_table = "mycat.myschema.population",
        mutation_agent   = "$MUTATION_AGENT_URL",
        fitness_agents   = ["$EVAL_AGENT_URL"],
        judge_agent      = "$JUDGE_AGENT_URL",
        warehouse_id     = "$DATABRICKS_WAREHOUSE_ID",
    ))
    app = create_app(loop)
"""
from .loop_agent import (
    LoopAgent,
    LoopConfig,
    Hypothesis,
    GenerationResult,
    CipherType,
    SourceLanguage,
)
from .population_store import PopulationStore, pareto_frontier, pareto_dominates
from .engine import (
    WorkflowEngine,
    RunStatus,
    RunSnapshot,
    RunSummary,
    RunFilter,
    StepRecord,
    StepFailedError,
)
from .engine_memory import InMemoryEngine
from .engine_delta import DeltaEngine

__all__ = [
    "LoopAgent",
    "LoopConfig",
    "Hypothesis",
    "GenerationResult",
    "CipherType",
    "SourceLanguage",
    "PopulationStore",
    "pareto_frontier",
    "pareto_dominates",
    # Durable execution
    "WorkflowEngine",
    "InMemoryEngine",
    "DeltaEngine",
    "RunStatus",
    "RunSnapshot",
    "RunSummary",
    "RunFilter",
    "StepRecord",
    "StepFailedError",
]
