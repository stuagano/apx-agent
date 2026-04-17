"""
Orchestrator Agent — loop controller and human review interface.

Owns the evolutionary loop lifecycle. Dispatches to sub-agents.
Manages the Delta Lake population. Surfaces results in Databricks Apps.

This is the agent that researchers interact with directly:
  "Whatf's the current best hypothesis?"
  "Show me generation 142 Pareto frontier"
  "Pause the loop and escalate candidate abc123 for expert review"
  "Inject constraint: force herbal section to Latin only"

Also callable via Databricks Workflows for automated generation runs.
"""
import json
import os
from typing import Annotated

from apx_agent import Agent, Dependencies, create_app

# LoopAgent is installed as part of the apx-agent-loop package.
# Install with: pip install apx-agent-loop
# In Databricks Apps: add to requirements.txt or pyproject.toml dependencies.
from apx_agent.workflow import LoopAgent, LoopConfig

_CATALOG = os.getenv("VOYNICH_CATALOG", "serverless_stable_s0v155_catalog")


# ---------------------------------------------------------------------------
# LoopConfig from environment (set in pyproject.toml [tool.apx.agent])
# ---------------------------------------------------------------------------

def _build_config() -> LoopConfig:
    return LoopConfig(
        population_table  = os.getenv("VOYNICH_POPULATION_TABLE", f"{_CATALOG}.voynich_evolution.population"),
        fitness_agents    = [
            os.getenv("HISTORIAN_AGENT_URL", ""),
            os.getenv("CRITIC_AGENT_URL",    ""),
        ],
        mutation_agent    = os.getenv("DECIPHERER_AGENT_URL", ""),
        judge_agent       = os.getenv("JUDGE_AGENT_URL",      ""),
        review_table      = os.getenv("VOYNICH_REVIEW_TABLE", f"{_CATALOG}.voynich_evolution.review_queue"),
        warehouse_id      = os.getenv("DATABRICKS_WAREHOUSE_ID", ""),
        population_size   = int(os.getenv("POPULATION_SIZE",    "500")),
        mutation_batch    = int(os.getenv("MUTATION_BATCH",     "50")),
        max_generations   = int(os.getenv("MAX_GENERATIONS",    "2000")),
        escalation_threshold = float(os.getenv("ESCALATION_THRESHOLD", "0.85")),
        mlflow_experiment = os.getenv("MLFLOW_EXPERIMENT", "/voynich/evolutionary_search"),
    )


loop = LoopAgent(config=_build_config())


# ---------------------------------------------------------------------------
# Orchestrator tools (conversational interface + Workflows entrypoints)
# ---------------------------------------------------------------------------

def run_generation_batch(
    n_generations: Annotated[int, "Number of generations to run in this batch"] = 10,
    ws: Dependencies.Workspace = None,
) -> dict:
    """
    Run N generations of the evolutionary loop. Called by Databricks Workflows.
    Dispatches to Decipherer, Historian, Critic, and Judge sub-agents per generation.
    Returns a summary of all generations completed in this batch.
    """
    import asyncio

    async def _run():
        results = []
        for i in range(n_generations):
            if not loop._running and i > 0:
                break
            loop._running = True
            from apx_agent.workflow import PopulationStore
            store = PopulationStore(ws, loop.config)
            store.ensure_schema()
            gen = loop._current_generation + i
            result = await loop._run_generation(gen, store, ws, f"batch_{i}")
            results.append(result)
            loop._current_generation = gen + 1
        return results

    results = asyncio.run(_run())

    return {
        "generations_run": len(results),
        "best_fitness": max((r.best_fitness for r in results), default=0.0),
        "total_escalated": sum(len(r.escalated) for r in results),
        "converged": any(r.converged for r in results),
        "generation_summaries": [
            {
                "generation": r.generation,
                "best_fitness": r.best_fitness,
                "pareto_size": r.pareto_frontier_size,
                "escalated": len(r.escalated),
                "wall_time_s": round(r.wall_time_s, 2),
            }
            for r in results
        ],
    }


def get_population_stats(
    generation: Annotated[int, "Generation to query (-1 = latest)"] = -1,
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Get statistics about the current population: fitness distribution,
    Pareto frontier composition, cipher type diversity, language distribution.
    """
    gen_filter = (
        f"(SELECT MAX(generation) FROM {_CATALOG}.voynich_evolution.population)"
        if generation == -1
        else str(generation)
    )

    stats = sql.execute(ff""f"
        SELECT
            COUNT(*) as total,
            AVG(fitness_composite) as avg_fitness,
            MAX(fitness_composite) as best_fitness,
            MIN(fitness_composite) as worst_fitness,
            STDDEV(fitness_composite) as fitness_stddev,
            COUNT(DISTINCT cipher_type) as cipher_type_diversity,
            COUNT(DISTINCT source_language) as language_diversity,
            SUM(CASE WHEN flagged_for_review THEN 1 ELSE 0 END) as flagged_count
        FROM {_CATALOG}.voynich_evolution.population
        WHERE generation = {gen_filter}
    """)

    breakdown = sql.execute(ff""f"
        SELECT cipher_type, source_language, COUNT(*) as count,
               AVG(fitness_composite) as avg_fitness
        FROM {_CATALOG}.voynich_evolution.population
        WHERE generation = {gen_filter}
        GROUP BY cipher_type, source_language
        ORDER BY avg_fitness DESC
        LIMIT 20
    """)

    return {
        "generation": generation,
        "population_stats": dict(stats[0]) if stats else {},
        "type_language_breakdown": [dict(r) for r in breakdown],
    }


def get_top_candidates(
    n: Annotated[int, "Number of top candidates to return"] = 10,
    section_filter: Annotated[str, "Filter by section or 'all'"] = "all",
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Return the top-N candidates by composite fitness from the latest generation.
    Includes decoded sample and full fitness vector for researcher review.
    """
    section_clause = "" if section_filter == "all" else f"AND section = '{section_filter}f'"
    rows = sql.execute(ff""f"
        SELECT *
        FROM {_CATALOG}.voynich_evolution.population
        WHERE generation = (SELECT MAX(generation) FROM {_CATALOG}.voynich_evolution.population)
        {section_clause}
        ORDER BY fitness_composite DESC
        LIMIT {n}
    """)
    return {
        "top_candidates": [dict(r) for r in rows],
        "count": len(rows),
        "generation": rows[0].get("generation") if rows else None,
    }


def inject_constraint(
    constraint_type: Annotated[str, "Type: force_language | ban_cipher_type | fix_symbol | require_section_vocab"],
    constraint_value: Annotated[str, "The constraint value (e.g. 'latin' for force_language)"],
    target_section: Annotated[str, "Section to apply constraint to, or 'allf'"] = "all",
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Inject a researcher-provided constraint into the evolutionary loop.
    Constraints are logged to Delta and picked up by the Decipherer on the next generation.
    Example: a medievalist determines the herbal section must be Latin → inject force_language=latin.
    """
    sql.execute(ff""f"
        INSERT INTO {_CATALOG}.voynich_evolution.constraints
        (constraint_type, constraint_value, target_section, active, created_by, created_at)
        VALUES (
            '{constraint_type}', '{constraint_value}',
            '{target_section}', true, 'researcher_uif',
            current_timestamp()
        )
    """)

    return {
        "constraint_injected": True,
        "type": constraint_type,
        "value": constraint_value,
        "section": target_section,
        "note": "Constraint will be active from next generation. Decipherer reads constraints table on startup.",
    }


def list_active_constraints(sql: Dependencies.Sql = None) -> dict:
    """List all active researcher-injected constraints currently influencing the loop."""
    rows = sql.execute(f""f"
        SELECT * FROM {_CATALOG}.voynich_evolution.constraints
        WHERE active = true
        ORDER BY created_at DESC
    """)
    return {
        "active_constraints": [dict(r) for r in rows],
        "count": len(rows),
    }


def flag_for_expert_review(
    hypothesis_id: Annotated[str, "Hypothesis ID to flag"],
    reason: Annotated[str, "Reason for escalation"],
    expert_type: Annotated[str, "Expert needed: cryptographer | medievalist | botanist | astronomer"] = "cryptographer",
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Flag a specific hypothesis for expert human review.
    Writes to the review queue table surfaced in the Databricks Apps UI.
    """
    sql.execute(ff""f"
        INSERT INTO {_CATALOG}.voynich_evolution.review_queue
        (hypothesis_id, reason, expert_type, status, flagged_at)
        VALUES (
            '{hypothesis_id}', '{reason.replace(chr(39), chr(34))}',
            '{expert_type}', 'pendingf', current_timestamp()
        )
    """)
    return {
        "flagged": True,
        "hypothesis_id": hypothesis_id,
        "expert_type": expert_type,
        "note": "Hypothesis is now visible in the Databricks Apps review queue.",
    }


def get_review_queue(sql: Dependencies.Sql = None) -> dict:
    """Get all hypotheses awaiting human expert review."""
    rows = sql.execute(f""f"
        SELECT r.*, p.fitness_composite, p.cipher_type, p.source_language,
               p.decoded_sample, p.agent_eval_historian, p.agent_eval_critic
        FROM {_CATALOG}.voynich_evolution.review_queue r
        JOIN {_CATALOG}.voynich_evolution.population p ON r.hypothesis_id = p.id
        WHERE r.status = 'pending'
        ORDER BY p.fitness_composite DESC
    """)
    return {
        "review_queue": [dict(r) for r in rows],
        "pending_count": len(rows),
    }


def get_agent_eval_summary(
    n_generations: Annotated[int, "Look back N generations"] = 10,
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Get a summary of agent eval scores across recent generations.
    Used to identify agents that need prompt refinement.
    """
    rows = sql.execute(ff""f"
        SELECT agent_name,
               AVG(composite_eval_score) as avg_score,
               MIN(composite_eval_score) as min_score,
               COUNT(*) as eval_count,
               SUM(CASE WHEN action_triggered != 'OKf' THEN 1 ELSE 0 END) as issues
        FROM {_CATALOG}.voynich_evolution.agent_evals
        WHERE generation >= (SELECT MAX(generation) - {n_generations}
                             FROM {_CATALOG}.voynich_evolution.agent_evals)
        GROUP BY agent_name
        ORDER BY avg_score ASC
    """)
    return {
        "agent_health": [dict(r) for r in rows],
        "lookback_generations": n_generations,
        "flagged_agents": [
            dict(r) for r in rows
            if float(r.get("avg_score", 1.0)) < 0.5
        ],
    }


def get_evolutionary_trajectory(sql: Dependencies.Sql = None) -> dict:
    """
    Get the fitness trajectory across all completed generations.
    Used to visualize progress in the Databricks Apps UI.
    """
    rows = sql.execute(f""f"
        SELECT
            generation,
            MAX(fitness_composite) as best_fitness,
            AVG(fitness_composite) as avg_fitness,
            COUNT(*) as population_size,
            SUM(CASE WHEN flagged_for_review THEN 1 ELSE 0 END) as escalated
        FROM {_CATALOG}.voynich_evolution.population
        GROUP BY generation
        ORDER BY generation ASC
    """)
    return {
        "trajectory": [dict(r) for r in rows],
        "total_generations": len(rows),
        "overall_best": max((r["best_fitness"] for r in rows), default=0.0),
    }


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

agent = Agent(
    tools=[
        # Loop control
        run_generation_batch,
        loop.get_status,
        loop.pause_loop,
        loop.resume_loop,
        loop.force_escalate,
        # Population inspection
        get_population_stats,
        get_top_candidates,
        get_evolutionary_trajectory,
        # Researcher interaction
        inject_constraint,
        list_active_constraints,
        flag_for_expert_review,
        get_review_queue,
        # Agent health
        get_agent_eval_summary,
    ],
    sub_agents=[
        os.getenv("DECIPHERER_AGENT_URL", ""),
        os.getenv("HISTORIAN_AGENT_URL",  ""),
        os.getenv("CRITIC_AGENT_URL",     ""),
        os.getenv("JUDGE_AGENT_URL",      ""),
    ],
    instructions="""
You are the Orchestrator Agent for the Voynich evolutionary cryptanalysis system.
You control the loop, manage the population, and serve as the researcher's primary interface.

You have two modes:
1. AUTOMATED (called by Databricks Workflows): run_generation_batch() for N generations.
2. INTERACTIVE (researcher in Databricks Apps): answer questions, show results, accept constraints.

In automated mode:
- Call run_generation_batch() for the requested number of generations.
- Report the summary including best fitness, escalations, and convergence status.
- If converged, flag for researcher review and halt.

In interactive mode (researcher asks questions):
- "What's the status?" → get_status() + get_evolutionary_trajectory()
- "Show best candidates" → get_top_candidates()
- "What's the Pareto frontier?" → get_population_stats() 
- "Pause the loop" → pause_loop()
- "Force Latin for herbal section" → inject_constraint(force_language, latin, herbal)
- "Flag candidate X for expert review" → flag_for_expert_review()
- "Are any agents struggling?" → get_agent_eval_summary()
- "Show review queue" → get_review_queue()

The A2A sub-agents (Decipherer, Historian, Critic, Judge) are available
at their registered endpoints. You can call them directly for ad-hoc tasks
outside the normal generation loop.

Be concise and structured. Researchers are domain experts who want data, not prose.
""",
)

app = create_app(agent)
