"""
loop_agent.py — LoopAgent: a new apx-agent workflow primitive.

Extends the existing Sequential / Parallel / Loop / Router / Handoff vocabulary
with generation-level population management. Unlike the existing Loop agent
(which iterates a single request), LoopAgent manages a *population* of
hypothesis states across *generations*, persisting everything in Delta Lake.

Analogous to ADK's Runner but with:
  - Delta Lake population persistence (ACID, time-travel)
  - Multi-objective Pareto-frontier selection
  - Pluggable convergence detection
  - Human escalation gates via Databricks Apps
  - MLflow generation-level experiment tracking

Usage:
    from apx_agent import Dependencies, create_app
    from loop_agent import LoopAgent, LoopConfig, Hypothesis

    loop = LoopAgent(
        config=LoopConfig(
            population_table="voynich.evolution.population",
            fitness_agents=["$HISTORIAN_URL", "$CRITIC_URL", "$JUDGE_URL"],
            mutation_agent="$DECIPHERER_URL",
        ),
        tools=[run_generation, inspect_population, force_escalate],
    )
    app = create_app(loop)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import mlflow
from databricks.sdk import WorkspaceClient


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class CipherType(str):
    SUBSTITUTION   = "substitution"
    TRANSPOSITION  = "transposition"
    POLYALPHABETIC = "polyalphabetic"
    NULL_BEARING   = "null_bearing"
    COMPOSITE      = "composite"
    STEGANOGRAPHIC = "steganographic"

class SourceLanguage(str):
    LATIN      = "latin"
    HEBREW     = "hebrew"
    ARABIC     = "arabic"
    ITALIAN    = "italian"
    OCCITAN    = "occitan"
    CONSTRUCTED = "constructed"


@dataclass
class Hypothesis:
    """A single cipher hypothesis — the genome of the evolutionary system."""
    id: str                            = field(default_factory=lambda: str(uuid.uuid4())[:8])
    generation: int                    = 0
    parent_id: str | None              = None
    cipher_type: str                   = CipherType.SUBSTITUTION
    source_language: str               = SourceLanguage.LATIN
    symbol_map: dict[str, str]         = field(default_factory=dict)
    null_chars: list[str]              = field(default_factory=list)
    transformation_rules: list[dict]   = field(default_factory=list)

    # Fitness signals — populated by evaluator agents
    fitness_statistical: float         = 0.0
    fitness_perplexity: float          = 0.0
    fitness_semantic: float            = 0.0
    fitness_consistency: float         = 0.0
    fitness_adversarial: float         = 0.0   # populated only for top-5%
    fitness_composite: float           = 0.0

    # Agent eval scores — populated by Judge agent
    agent_eval_historian: float        = 0.0
    agent_eval_critic: float           = 0.0

    # Outputs
    decoded_sample: str                = ""
    mlflow_run_id: str                 = ""
    flagged_for_review: bool           = False

    def composite_fitness(self) -> float:
        return (
            0.25 * self.fitness_statistical
            + 0.25 * self.fitness_perplexity
            + 0.30 * self.fitness_semantic
            + 0.15 * self.fitness_consistency
            + 0.05 * self.fitness_adversarial
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "generation": self.generation,
            "parent_id": self.parent_id,
            "cipher_type": self.cipher_type,
            "source_language": self.source_language,
            "symbol_map": json.dumps(self.symbol_map),
            "null_chars": json.dumps(self.null_chars),
            "transformation_rules": json.dumps(self.transformation_rules),
            "fitness_statistical": self.fitness_statistical,
            "fitness_perplexity": self.fitness_perplexity,
            "fitness_semantic": self.fitness_semantic,
            "fitness_consistency": self.fitness_consistency,
            "fitness_adversarial": self.fitness_adversarial,
            "fitness_composite": self.composite_fitness(),
            "agent_eval_historian": self.agent_eval_historian,
            "agent_eval_critic": self.agent_eval_critic,
            "decoded_sample": self.decoded_sample,
            "mlflow_run_id": self.mlflow_run_id,
            "flagged_for_review": self.flagged_for_review,
        }

    _FLOAT_FIELDS = frozenset({
        "fitness_statistical", "fitness_perplexity", "fitness_semantic",
        "fitness_consistency", "fitness_adversarial", "fitness_composite",
        "agent_eval_historian", "agent_eval_critic",
    })
    _INT_FIELDS = frozenset({"generation"})

    @classmethod
    def from_dict(cls, d: dict) -> "Hypothesis":
        h = cls()
        for k, v in d.items():
            if not hasattr(h, k):
                continue
            if k in ("symbol_map", "null_chars", "transformation_rules") and isinstance(v, str):
                v = json.loads(v) if v else ({} if k == "symbol_map" else [])
            elif k in cls._FLOAT_FIELDS:
                try:
                    v = float(v) if v is not None else 0.0
                except (TypeError, ValueError):
                    v = 0.0
            elif k in cls._INT_FIELDS:
                try:
                    v = int(v) if v is not None else 0
                except (TypeError, ValueError):
                    v = 0
            elif k == "flagged_for_review":
                v = bool(v) if not isinstance(v, str) else v.lower() in ("true", "1")
            setattr(h, k, v)
        return h


from .population_store import PopulationStore, pareto_frontier  # noqa: E402


@dataclass
class GenerationResult:
    generation: int
    population_size: int
    best_fitness: float
    pareto_frontier_size: int
    survivors: list[Hypothesis]
    escalated: list[Hypothesis]
    wall_time_s: float
    converged: bool = False


@dataclass
class LoopConfig:
    """All config for a LoopAgent instance — maps to [tool.apx.loop] in pyproject.toml."""
    population_table: str              # Delta FQN: catalog.schema.table
    fitness_agents: list[str]          # sub-agent URLs for evaluators
    mutation_agent: str                # sub-agent URL for Decipherer
    judge_agent: str                   # sub-agent URL for Judge
    review_table: str = ""             # Delta FQN: catalog.schema.review_queue
    warehouse_id: str = ""             # SQL warehouse for Delta ops
    population_size: int = 500
    mutation_batch: int = 50           # new hypotheses per generation
    max_generations: int = 2000
    convergence_patience: int = 50     # gens without improvement → converged
    escalation_threshold: float = 0.85 # composite fitness to flag for review
    top_k_adversarial: float = 0.05    # fraction to run adversarial eval on
    pareto_objectives: list[str] = field(default_factory=lambda: [
        "fitness_statistical", "fitness_perplexity",
        "fitness_semantic", "fitness_consistency",
    ])
    mlflow_experiment: str = "/voynich/evolutionary_search"


# ---------------------------------------------------------------------------
# LoopAgent
# ---------------------------------------------------------------------------

class LoopAgent:
    """
    New apx-agent workflow primitive: manages an evolutionary loop over
    a population of hypotheses, persisting state in Delta Lake between
    generations. Each generation fans out to sub-agents for mutation,
    evaluation, and judge scoring.

    Exposes its own set of apx-agent tools so it can itself be queried
    conversationally (e.g. "what's the current best hypothesis?",
    "pause the loop", "force escalation of candidate abc123").

    Integrates with apx-agent's create_app() — drop-in replacement for Agent().
    """

    def __init__(self, config: LoopConfig, extra_tools: list[Callable] | None = None):
        self.config = config
        self._extra_tools = extra_tools or []
        self._running = False
        self._current_generation = 0
        self._results: list[GenerationResult] = []

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    async def run(self, ws: WorkspaceClient):
        """Entry point — called by Databricks Workflows task or direct invocation."""
        store = PopulationStore(ws, self.config)
        store.ensure_schema()

        mlflow.set_experiment(self.config.mlflow_experiment)
        self._running = True

        with mlflow.start_run(run_name="evolutionary_search") as parent_run:
            mlflow.log_params({
                "population_size": self.config.population_size,
                "max_generations": self.config.max_generations,
                "pareto_objectives": ",".join(self.config.pareto_objectives),
            })

            for gen in range(self.config.max_generations):
                if not self._running:
                    break

                self._current_generation = gen
                result = await self._run_generation(gen, store, ws, parent_run.info.run_id)
                self._results.append(result)

                mlflow.log_metrics({
                    "best_fitness": result.best_fitness,
                    "pareto_size": result.pareto_frontier_size,
                    "escalated_count": len(result.escalated),
                    "wall_time_s": result.wall_time_s,
                }, step=gen)

                if result.converged:
                    mlflow.set_tag("termination", "converged")
                    break

        self._running = False

    async def _run_generation(
        self,
        generation: int,
        store: PopulationStore,
        ws: WorkspaceClient,
        parent_run_id: str,
    ) -> GenerationResult:
        t0 = time.monotonic()

        with mlflow.start_run(run_name=f"gen_{generation:04d}", nested=True) as run:
            # 1. Load parents (empty on gen 0 → seed random population)
            parents = store.load_pareto_survivors(generation - 1, self.config.mutation_batch) \
                if generation > 0 else []

            # 2. Mutate / generate new candidates
            candidates = await self._mutate(parents, generation)

            # 3. Fan-out evaluation to fitness agents (parallel)
            evaluated = await self._evaluate(candidates, ws)

            # 4. Judge agent scores reasoning traces
            judged = await self._judge(evaluated, ws)

            # 5. Pareto selection from (parents ∪ evaluated)
            full_pool = parents + judged
            frontier = pareto_frontier(full_pool, self.config.pareto_objectives)
            survivors = frontier[:self.config.population_size]

            # 6. Flag high-fitness candidates for human review
            escalated = [
                h for h in survivors
                if h.composite_fitness() >= self.config.escalation_threshold
                and not h.flagged_for_review
            ]
            for h in escalated:
                h.flagged_for_review = True

            # 7. Write generation to Delta
            for h in judged:
                h.mlflow_run_id = run.info.run_id
            store.write_hypotheses(judged)

            # 8. Convergence check
            history = store.get_best_fitness_history(self.config.convergence_patience)
            best = max((h.composite_fitness() for h in survivors), default=0.0)
            converged = (
                len(history) >= self.config.convergence_patience
                and max(history) - min(history) < 0.001
            )

            mlflow.log_metrics({
                "candidates_generated": len(candidates),
                "frontier_size": len(frontier),
                "best_composite": best,
            })

        return GenerationResult(
            generation=generation,
            population_size=len(survivors),
            best_fitness=best,
            pareto_frontier_size=len(frontier),
            survivors=survivors,
            escalated=escalated,
            wall_time_s=time.monotonic() - t0,
            converged=converged,
        )

    # ------------------------------------------------------------------
    # Sub-agent dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def _url_to_app_name(url: str) -> str | None:
        """Extract Databricks App name from URL hostname."""
        from urllib.parse import urlparse
        if not url or "databricksapps.com" not in url:
            return None
        try:
            host = urlparse(url).hostname or ""
            name_with_id = host.split(".")[0]
            segments = name_with_id.split("-")
            for i in range(len(segments) - 1, 0, -1):
                if segments[i].isdigit() and len(segments[i]) > 8:
                    return "-".join(segments[:i])
            return name_with_id
        except Exception:
            return None

    @staticmethod
    async def _call_app(app_name: str, content: str, timeout: float = 120.0) -> str:
        """Call a Databricks App via DatabricksOpenAI SDK (handles M2M auth automatically).

        Requires OAuth M2M: set DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET in the job
        environment. Raises if auth is not configured — callers should catch and handle.
        """
        from databricks_openai import AsyncDatabricksOpenAI
        client = AsyncDatabricksOpenAI()
        response = await client.responses.create(
            model=f"apps/{app_name}",
            input=[{"type": "message", "role": "user", "content": content}],
        )
        return response.output_text

    async def _mutate(self, parents: list[Hypothesis], generation: int,
                      ws: WorkspaceClient | None = None) -> list[Hypothesis]:
        """Call Decipherer sub-agent to generate new candidates.

        Returns [] if the agent is unreachable or auth is not configured —
        callers should fall back to local random seeding.
        """
        app_name = self._url_to_app_name(self.config.mutation_agent)
        if not app_name:
            return []
        content = json.dumps({
            "task": "generate_mutations",
            "generation": generation,
            "parents": [p.to_dict() for p in parents],
            "n": self.config.mutation_batch,
            "population_size": self.config.population_size,
        })
        try:
            raw = await self._call_app(app_name, content, timeout=60.0)
            hypotheses_raw = json.loads(raw) if raw.startswith("[") else json.loads(raw).get("hypotheses", [])
            return [Hypothesis.from_dict(h) for h in hypotheses_raw]
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Decipherer agent call failed (returning []): %s — "
                "Tip: set DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET for OAuth M2M", exc
            )
            return []

    async def _evaluate(self, candidates: list[Hypothesis], ws: WorkspaceClient) -> list[Hypothesis]:
        """Fan out evaluation tasks to fitness agent sub-agents in parallel."""
        # Identify top-K% for adversarial eval (run stat eval first to rank)
        stat_tasks = [self._call_fitness_agent(h, "statistical") for h in candidates]
        stat_results = await asyncio.gather(*stat_tasks, return_exceptions=True)

        for h, result in zip(candidates, stat_results):
            if isinstance(result, Exception):
                continue
            h.fitness_statistical = result.get("score", 0.0)

        # Sort by stat score to identify top-K for adversarial
        ranked = sorted(candidates, key=lambda h: h.fitness_statistical, reverse=True)
        adversarial_cutoff = max(1, int(len(ranked) * self.config.top_k_adversarial))
        adversarial_set = set(h.id for h in ranked[:adversarial_cutoff])

        # Now run all other evaluators in parallel
        all_tasks = []
        for h in candidates:
            all_tasks.append(self._call_fitness_agent(h, "historian"))
            all_tasks.append(self._call_fitness_agent(h, "critic"))
            if h.id in adversarial_set:
                all_tasks.append(self._call_fitness_agent(h, "adversarial"))

        await asyncio.gather(*all_tasks, return_exceptions=True)
        return candidates

    async def _call_fitness_agent(self, hypothesis: Hypothesis, agent_type: str) -> dict:
        """Call a single fitness agent endpoint for one hypothesis. Returns {} on failure."""
        agent_url_map = {
            "historian":   self.config.fitness_agents[0] if self.config.fitness_agents else "",
            "critic":      self.config.fitness_agents[1] if len(self.config.fitness_agents) > 1 else "",
            "adversarial": self.config.fitness_agents[1] if len(self.config.fitness_agents) > 1 else "",
        }
        url = agent_url_map.get(agent_type, "")
        app_name = self._url_to_app_name(url)
        if not app_name:
            return {}
        content = json.dumps({
            "task": f"evaluate_{agent_type}",
            "hypothesis": hypothesis.to_dict(),
        })
        try:
            raw = await self._call_app(app_name, content)
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return {}

    async def _judge(self, evaluated: list[Hypothesis], ws: WorkspaceClient) -> list[Hypothesis]:
        """Call Judge agent to score reasoning trace quality for top candidates."""
        app_name = self._url_to_app_name(self.config.judge_agent)
        if not app_name:
            return evaluated

        top_n = max(1, int(len(evaluated) * 0.20))  # judge top 20%
        ranked = sorted(evaluated, key=lambda h: h.composite_fitness(), reverse=True)
        top = ranked[:top_n]

        for h in top:
            try:
                raw = await self._call_app(app_name, json.dumps({
                    "task": "score_reasoning",
                    "hypothesis_id": h.id,
                    "mlflow_run_id": h.mlflow_run_id,
                }))
                data = json.loads(raw) if isinstance(raw, str) else raw
                h.agent_eval_historian = data.get("historian_score", 0.0)
                h.agent_eval_critic    = data.get("critic_score", 0.0)
            except Exception:
                pass  # non-blocking — judge eval is best-effort

        return evaluated

    # ------------------------------------------------------------------
    # apx-agent tool surface (conversational interface to the loop)
    # ------------------------------------------------------------------

    def tools(self) -> list[Callable]:
        """Return tools that apx-agent registers on this LoopAgent's app."""
        return [
            self.get_status,
            self.get_best_hypothesis,
            self.get_generation_summary,
            self.pause_loop,
            self.resume_loop,
            self.force_escalate,
            *self._extra_tools,
        ]

    def get_status(self) -> dict:
        """Get the current status of the evolutionary loop."""
        return {
            "running": self._running,
            "current_generation": self._current_generation,
            "generations_completed": len(self._results),
            "best_fitness": max(
                (r.best_fitness for r in self._results), default=0.0
            ),
            "total_escalated": sum(len(r.escalated) for r in self._results),
        }

    def get_best_hypothesis(self, generation: int = -1) -> dict:
        """Get the best hypothesis from a given generation (-1 = latest)."""
        if not self._results:
            return {"error": "no generations completed yet"}
        result = self._results[generation]
        if not result.survivors:
            return {"error": "empty population"}
        best = max(result.survivors, key=lambda h: h.composite_fitness())
        return best.to_dict()

    def get_generation_summary(self, generation: int = -1) -> dict:
        """Get a summary of a completed generation."""
        if not self._results:
            return {"error": "no generations completed yet"}
        r = self._results[generation]
        return {
            "generation": r.generation,
            "best_fitness": r.best_fitness,
            "pareto_frontier_size": r.pareto_frontier_size,
            "escalated": len(r.escalated),
            "wall_time_s": round(r.wall_time_s, 2),
            "converged": r.converged,
        }

    def pause_loop(self) -> dict:
        """Pause the evolutionary loop after the current generation completes."""
        self._running = False
        return {"paused": True, "at_generation": self._current_generation}

    def resume_loop(self) -> dict:
        """Resume a paused loop. Note: caller must re-invoke run() as a task."""
        return {"message": "Re-dispatch the Workflows job to resume the loop.", 
                "from_generation": self._current_generation}

    def force_escalate(self, hypothesis_id: str) -> dict:
        """Force a specific hypothesis to be flagged for human review."""
        for result in self._results:
            for h in result.survivors:
                if h.id == hypothesis_id:
                    h.flagged_for_review = True
                    return {"flagged": True, "hypothesis_id": hypothesis_id}
        return {"error": f"hypothesis {hypothesis_id} not found"}
