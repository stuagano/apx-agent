"""
loop_agent/cli.py — CLI entry points for Databricks Workflows.

These are the functions referenced as python_wheel_task entry_points in the
Workflow YAML. Each accepts named_parameters as CLI args and runs as a
self-contained Databricks task.

Entry points (defined in pyproject.toml [project.scripts]):
    voynich-seed         → seed_population()
    voynich-run          → run_generation_batch()
    voynich-vacuum       → vacuum_population()
    voynich-export       → export_top_candidates()

Usage in Workflow YAML:
    python_wheel_task:
      package_name: "apx-agent-loop"
      entry_point: "voynich-run"
      named_parameters:
        n_generations: "500"
        from_generation: "0"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def _build_config_from_env():
    """Build LoopConfig from environment variables. Called by all CLI entry points."""
    from loop_agent import LoopConfig
    return LoopConfig(
        population_table     = os.environ["VOYNICH_POPULATION_TABLE"],
        fitness_agents       = [
            url.strip()
            for url in os.environ.get("FITNESS_AGENT_URLS", "").split(",")
            if url.strip()
        ],
        mutation_agent       = os.environ["DECIPHERER_AGENT_URL"],
        judge_agent          = os.environ["JUDGE_AGENT_URL"],
        review_table         = os.environ.get("VOYNICH_REVIEW_TABLE", "voynich.evolution.review_queue"),
        warehouse_id         = os.environ["DATABRICKS_WAREHOUSE_ID"],
        population_size      = int(os.environ.get("POPULATION_SIZE",    "500")),
        mutation_batch       = int(os.environ.get("MUTATION_BATCH",     "50")),
        max_generations      = int(os.environ.get("MAX_GENERATIONS",    "2000")),
        escalation_threshold = float(os.environ.get("ESCALATION_THRESHOLD", "0.85")),
        mlflow_experiment    = os.environ.get("MLFLOW_EXPERIMENT", "/voynich/evolutionary_search"),
    )


def _build_workspace_client():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()


# ---------------------------------------------------------------------------
# seed_population
# ---------------------------------------------------------------------------

def seed_population():
    """
    Entry point: voynich-seed
    Generates the initial population (generation 0) via the Decipherer agent.

    Named parameters (Workflow):
        n: int = 500        number of seed hypotheses
    """
    parser = argparse.ArgumentParser(description="Seed Voynich evolutionary population")
    parser.add_argument("--n", type=int, default=500, help="Number of seed hypotheses")
    args = parser.parse_args()

    print(f"[voynich-seed] Seeding population with {args.n} hypotheses...")

    config = _build_config_from_env()
    ws     = _build_workspace_client()

    from loop_agent import LoopAgent, PopulationStore

    store = PopulationStore(ws, config)
    store.ensure_schema()

    loop = LoopAgent(config=config)

    # Call Decipherer to generate seed hypotheses
    seed_hypotheses = asyncio.run(loop._mutate(parents=[], generation=0))
    if not seed_hypotheses:
        print("[voynich-seed] WARNING: Decipherer returned 0 hypotheses. Using fallback seeder.")
        # Fallback: generate minimal diversity seed using built-in logic
        from loop_agent.loop_agent import CipherType, SourceLanguage
        import uuid, random
        alphabet = list("abcdefghijklmnopqrstuvwxyz")
        seed_hypotheses = []
        for i in range(min(args.n, 500)):
            shuffled = alphabet[:]
            random.shuffle(shuffled)
            from loop_agent import Hypothesis
            seed_hypotheses.append(Hypothesis(
                id=str(uuid.uuid4())[:8],
                generation=0,
                cipher_type=list(CipherType.__dict__.values())[i % 6]
                            if hasattr(CipherType, '__dict__') else "substitution",
                source_language=["latin","hebrew","arabic","italian","occitan","catalan"][i % 6],
                symbol_map={c: shuffled[j % 26] for j, c in enumerate("oainshe")},
                null_chars=["q"] if i % 3 == 0 else [],
            ))

    store.write_hypotheses(seed_hypotheses[:args.n])
    print(f"[voynich-seed] ✓ Wrote {len(seed_hypotheses[:args.n])} hypotheses to generation 0")
    print(json.dumps({"status": "ok", "seeded": len(seed_hypotheses[:args.n])}))


# ---------------------------------------------------------------------------
# run_generation_batch
# ---------------------------------------------------------------------------

def run_generation_batch():
    """
    Entry point: voynich-run
    Runs N evolutionary generations, starting from the latest completed generation.

    Named parameters (Workflow):
        n_generations:   int = 500   how many generations to run
        from_generation: int = -1    start generation (-1 = auto-detect from Delta)
    """
    parser = argparse.ArgumentParser(description="Run Voynich evolutionary generations")
    parser.add_argument("--n_generations",   type=int, default=500, help="Generations to run")
    parser.add_argument("--from_generation", type=int, default=-1,  help="Starting generation (-1=auto)")
    args = parser.parse_args()

    print(f"[voynich-run] Running {args.n_generations} generations...")

    config = _build_config_from_env()
    ws     = _build_workspace_client()

    from loop_agent import LoopAgent, PopulationStore

    store = PopulationStore(ws, config)
    store.ensure_schema()

    # Detect starting generation
    start_gen = args.from_generation
    if start_gen < 0:
        history = store.get_best_fitness_history(n_generations=1)
        if history:
            rows = store._sql_exec(
                f"SELECT MAX(generation) as max_gen FROM {config.population_table}"
            )
            start_gen = int(rows[0]["max_gen"]) + 1 if rows else 0
        else:
            start_gen = 0

    print(f"[voynich-run] Starting from generation {start_gen}")

    loop    = LoopAgent(config=config)
    results = []

    import mlflow
    mlflow.set_experiment(config.mlflow_experiment)

    with mlflow.start_run(run_name=f"batch_gen{start_gen}_{start_gen + args.n_generations - 1}"):
        for i in range(args.n_generations):
            gen = start_gen + i
            loop._current_generation = gen
            loop._running = True

            result = asyncio.run(
                loop._run_generation(gen, store, ws, parent_run_id="batch")
            )
            results.append(result)

            mlflow.log_metrics({
                "best_fitness":       result.best_fitness,
                "pareto_size":        result.pareto_frontier_size,
                "escalated":          len(result.escalated),
                "wall_time_s":        result.wall_time_s,
            }, step=gen)

            print(
                f"[voynich-run] gen {gen:04d} | "
                f"best={result.best_fitness:.4f} | "
                f"pareto={result.pareto_frontier_size} | "
                f"escalated={len(result.escalated)} | "
                f"{result.wall_time_s:.1f}s"
            )

            if result.converged:
                print(f"[voynich-run] ✓ Converged at generation {gen}")
                mlflow.set_tag("termination_reason", "converged")
                break

    summary = {
        "status":            "ok",
        "generations_run":   len(results),
        "start_generation":  start_gen,
        "end_generation":    start_gen + len(results) - 1,
        "best_fitness":      max((r.best_fitness for r in results), default=0.0),
        "total_escalated":   sum(len(r.escalated) for r in results),
        "converged":         any(r.converged for r in results),
        "total_wall_time_s": round(sum(r.wall_time_s for r in results), 1),
    }
    print(json.dumps(summary))
    return summary


# ---------------------------------------------------------------------------
# vacuum_population
# ---------------------------------------------------------------------------

def vacuum_population():
    """
    Entry point: voynich-vacuum
    Runs OPTIMIZE + ZORDER on the population table, removes stale checkpoints.
    Run periodically (e.g. every 100 generations) to keep Delta performance healthy.

    Named parameters:
        keep_generations: int = 100   retain only top N generations
    """
    parser = argparse.ArgumentParser(description="Vacuum Voynich population table")
    parser.add_argument("--keep_generations", type=int, default=100,
                        help="Keep top-N generations, vacuum the rest")
    args = parser.parse_args()

    config = _build_config_from_env()
    ws     = _build_workspace_client()

    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    table = config.population_table
    print(f"[voynich-vacuum] Optimizing {table}...")

    # OPTIMIZE with ZORDER for the most common query pattern
    spark.sql(f"OPTIMIZE {table} ZORDER BY (generation, fitness_composite)")
    print(f"[voynich-vacuum] ✓ OPTIMIZE complete")

    # VACUUM (remove files older than 7 days)
    spark.sql(f"VACUUM {table} RETAIN 168 HOURS")
    print(f"[voynich-vacuum] ✓ VACUUM complete")

    # Remove flagged-for-review entries older than keep_generations
    rows = spark.sql(f"""
        SELECT COUNT(*) as cnt FROM {table}
        WHERE generation < (SELECT MAX(generation) - {args.keep_generations} FROM {table})
        AND flagged_for_review = false
    """).collect()
    stale = rows[0]["cnt"] if rows else 0
    print(f"[voynich-vacuum] {stale} stale non-flagged rows available for deletion")
    print(json.dumps({"status": "ok", "stale_rows": stale}))


# ---------------------------------------------------------------------------
# export_top_candidates
# ---------------------------------------------------------------------------

def export_top_candidates():
    """
    Entry point: voynich-export
    Exports top-N candidates from the latest generation as JSON to DBFS.
    Used to checkpoint results before a long batch run.

    Named parameters:
        n:           int = 20    number of candidates to export
        output_path: str         DBFS path for JSON output
    """
    parser = argparse.ArgumentParser(description="Export top Voynich candidates")
    parser.add_argument("--n",           type=int, default=20)
    parser.add_argument("--output_path", type=str,
                        default="/dbfs/voynich/exports/top_candidates.json")
    args = parser.parse_args()

    config = _build_config_from_env()
    ws     = _build_workspace_client()

    from loop_agent import PopulationStore
    store = PopulationStore(ws, config)

    candidates = store.load_pareto_survivors(generation=-1, top_n=args.n)

    import pathlib
    output = pathlib.Path(args.output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    export = {
        "n_candidates":    len(candidates),
        "export_timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        "candidates":      [c.to_dict() for c in candidates],
    }
    output.write_text(json.dumps(export, indent=2))
    print(f"[voynich-export] ✓ Exported {len(candidates)} candidates to {args.output_path}")
    print(json.dumps({"status": "ok", "path": args.output_path, "n": len(candidates)}))
