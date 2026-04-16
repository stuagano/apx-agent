"""
population_store.py — Delta Lake population manager.

Separates storage concerns from LoopAgent loop logic.

Two write paths:
  1. Spark (preferred, used from Workflows/notebooks) — bulk DataFrame write,
     no SQL length limits, handles 500+ hypotheses in one operation.
  2. SQL fallback (used from Apps / small writes) — statement_execution API,
     fine for single-row ops like constraint injection or review flagging.

The Spark path requires a running SparkSession (available in Workflows tasks
and notebooks). The SQL path works everywhere including Databricks Apps.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from .loop_agent import Hypothesis, LoopConfig


# ---------------------------------------------------------------------------
# Pareto selection (pure functions, no I/O)
# ---------------------------------------------------------------------------

def pareto_dominates(
    a: "Hypothesis",
    b: "Hypothesis",
    objectives: list[str],
) -> bool:
    """Return True if a dominates b: a >= b on all objectives and a > b on at least one."""
    a_vals = [getattr(a, o, 0.0) for o in objectives]
    b_vals = [getattr(b, o, 0.0) for o in objectives]
    return (
        all(av >= bv for av, bv in zip(a_vals, b_vals))
        and any(av > bv for av, bv in zip(a_vals, b_vals))
    )


def pareto_frontier(
    population: list["Hypothesis"],
    objectives: list[str],
) -> list["Hypothesis"]:
    """Extract non-dominated Pareto frontier. O(n²) — fine for n ≤ 1000."""
    frontier = []
    for candidate in population:
        dominated = any(
            pareto_dominates(other, candidate, objectives)
            for other in population
            if other is not candidate
        )
        if not dominated:
            frontier.append(candidate)
    return frontier


# ---------------------------------------------------------------------------
# PopulationStore
# ---------------------------------------------------------------------------

class PopulationStore:
    """
    Manages hypothesis population in Delta Lake.

    Spark path  → write_hypotheses_spark()   (bulk, Workflow/notebook context)
    SQL path    → write_hypotheses_sql()      (small writes, Apps context)
    Public API  → write_hypotheses()          (auto-selects based on availability)
    """

    def __init__(self, ws: "WorkspaceClient", config: "LoopConfig"):
        self.ws     = ws
        self.config = config
        self._spark = None  # lazy init

    # ------------------------------------------------------------------
    # Spark session (lazy, only available in Workflow/notebook context)
    # ------------------------------------------------------------------

    def _get_spark(self):
        if self._spark is not None:
            return self._spark
        try:
            from pyspark.sql import SparkSession
            self._spark = SparkSession.builder.getOrCreate()
            return self._spark
        except ImportError:
            return None

    def _has_spark(self) -> bool:
        return self._get_spark() is not None

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self):
        """Create population table if it doesn't exist. Idempotent."""
        self._sql_exec(f"""
            CREATE TABLE IF NOT EXISTS {self.config.population_table} (
                id                   STRING        NOT NULL,
                generation           INT           NOT NULL,
                parent_id            STRING,
                cipher_type          STRING,
                source_language      STRING,
                symbol_map           STRING,
                null_chars           STRING,
                transformation_rules STRING,
                fitness_statistical  DOUBLE        DEFAULT 0.0,
                fitness_perplexity   DOUBLE        DEFAULT 0.0,
                fitness_semantic     DOUBLE        DEFAULT 0.0,
                fitness_consistency  DOUBLE        DEFAULT 0.0,
                fitness_adversarial  DOUBLE        DEFAULT 0.0,
                fitness_composite    DOUBLE        DEFAULT 0.0,
                agent_eval_historian DOUBLE        DEFAULT 0.0,
                agent_eval_critic    DOUBLE        DEFAULT 0.0,
                decoded_sample       STRING,
                mlflow_run_id        STRING,
                flagged_for_review   BOOLEAN       DEFAULT FALSE,
                created_at           TIMESTAMP     DEFAULT current_timestamp()
            )
            USING DELTA
            PARTITIONED BY (generation)
            TBLPROPERTIES (
                'delta.enableChangeDataFeed'         = 'true',
                'delta.autoOptimize.optimizeWrite'   = 'true',
                'delta.autoOptimize.autoCompact'     = 'true'
            )
        """)

        # Constraints and review queue (owned by orchestrator, not loop)
        for ddl in [
            f"""
            CREATE TABLE IF NOT EXISTS {self.config.review_table} (
                hypothesis_id  STRING,
                reason         STRING,
                expert_type    STRING,
                status         STRING DEFAULT 'pending',
                annotation     STRING,
                flagged_at     TIMESTAMP DEFAULT current_timestamp(),
                resolved_at    TIMESTAMP
            ) USING DELTA
            """,
            """
            CREATE TABLE IF NOT EXISTS voynich.evolution.constraints (
                id               BIGINT GENERATED ALWAYS AS IDENTITY,
                constraint_type  STRING,
                constraint_value STRING,
                target_section   STRING,
                active           BOOLEAN DEFAULT TRUE,
                created_by       STRING,
                created_at       TIMESTAMP DEFAULT current_timestamp()
            ) USING DELTA
            """,
            """
            CREATE TABLE IF NOT EXISTS voynich.evolution.agent_evals (
                agent_name               STRING,
                hypothesis_id            STRING,
                generation               INT,
                tool_use_score           DOUBLE,
                reasoning_quality        DOUBLE,
                hallucination_confidence DOUBLE,
                composite_eval_score     DOUBLE,
                action_triggered         STRING,
                mlflow_run_id            STRING,
                created_at               TIMESTAMP DEFAULT current_timestamp()
            ) USING DELTA
            PARTITIONED BY (generation)
            """,
        ]:
            try:
                self._sql_exec(ddl)
            except Exception:
                pass  # table likely already exists

    # ------------------------------------------------------------------
    # Write path — Spark (bulk, preferred)
    # ------------------------------------------------------------------

    def write_hypotheses_spark(self, hypotheses: list["Hypothesis"]):
        """
        Bulk write via Spark DataFrame.write.mode('append').saveAsTable().

        Handles 500+ rows without hitting SQL statement length limits.
        Requires SparkSession (available in Workflow tasks and notebooks).
        Automatically applies Delta optimizeWrite for efficient small-file handling.
        """
        from pyspark.sql import SparkSession
        from pyspark.sql.types import (
            StructType, StructField,
            StringType, IntegerType, DoubleType, BooleanType, TimestampType,
        )

        schema = StructType([
            StructField("id",                   StringType(),  False),
            StructField("generation",            IntegerType(), False),
            StructField("parent_id",             StringType(),  True),
            StructField("cipher_type",           StringType(),  True),
            StructField("source_language",       StringType(),  True),
            StructField("symbol_map",            StringType(),  True),
            StructField("null_chars",            StringType(),  True),
            StructField("transformation_rules",  StringType(),  True),
            StructField("fitness_statistical",   DoubleType(),  True),
            StructField("fitness_perplexity",    DoubleType(),  True),
            StructField("fitness_semantic",      DoubleType(),  True),
            StructField("fitness_consistency",   DoubleType(),  True),
            StructField("fitness_adversarial",   DoubleType(),  True),
            StructField("fitness_composite",     DoubleType(),  True),
            StructField("agent_eval_historian",  DoubleType(),  True),
            StructField("agent_eval_critic",     DoubleType(),  True),
            StructField("decoded_sample",        StringType(),  True),
            StructField("mlflow_run_id",         StringType(),  True),
            StructField("flagged_for_review",    BooleanType(), True),
        ])

        spark = self._get_spark()
        rows  = [h.to_dict() for h in hypotheses]

        # Ensure JSON fields are strings (to_dict() already does this, but be safe)
        for row in rows:
            for col in ("symbol_map", "null_chars", "transformation_rules"):
                if not isinstance(row.get(col), str):
                    row[col] = json.dumps(row[col])
            # Drop created_at — Delta default fills it
            row.pop("created_at", None)

        df = spark.createDataFrame(rows, schema=schema)
        (
            df.write
            .format("delta")
            .mode("append")
            .option("mergeSchema", "false")      # schema is fixed
            .option("optimizeWrite", "true")     # auto-bin small files
            .saveAsTable(self.config.population_table)
        )

    # ------------------------------------------------------------------
    # Write path — SQL fallback (small writes / Apps context)
    # ------------------------------------------------------------------

    def write_hypotheses_sql(self, hypotheses: list["Hypothesis"], chunk_size: int = 25):
        """
        Write hypotheses via SQL statement_execution in chunks.
        Safe for Apps context where Spark is unavailable.
        chunk_size=25 keeps each SQL statement well under the 256 KB limit.
        """
        for i in range(0, len(hypotheses), chunk_size):
            chunk = hypotheses[i : i + chunk_size]
            self._sql_exec(self._build_insert(chunk))

    def _build_insert(self, hypotheses: list["Hypothesis"]) -> str:
        def _esc(v: str) -> str:
            return str(v).replace("'", "''")

        rows = []
        for h in hypotheses:
            rows.append(
                f"('{h.id}', {h.generation}, "
                f"{'NULL' if h.parent_id is None else repr(h.parent_id)}, "
                f"'{_esc(h.cipher_type)}', '{_esc(h.source_language)}', "
                f"'{_esc(json.dumps(h.symbol_map))}', "
                f"'{_esc(json.dumps(h.null_chars))}', "
                f"'{_esc(json.dumps(h.transformation_rules))}', "
                f"{h.fitness_statistical:.6f}, {h.fitness_perplexity:.6f}, "
                f"{h.fitness_semantic:.6f}, {h.fitness_consistency:.6f}, "
                f"{h.fitness_adversarial:.6f}, {h.composite_fitness():.6f}, "
                f"{h.agent_eval_historian:.6f}, {h.agent_eval_critic:.6f}, "
                f"'{_esc(h.decoded_sample[:500])}', '{_esc(h.mlflow_run_id)}', "
                f"{'true' if h.flagged_for_review else 'false'})"
            )
        return f"INSERT INTO {self.config.population_table} VALUES {', '.join(rows)}"

    # ------------------------------------------------------------------
    # Auto-selecting write path
    # ------------------------------------------------------------------

    def write_hypotheses(self, hypotheses: list["Hypothesis"]):
        """
        Write hypotheses using the best available path:
          - Spark (preferred) when SparkSession is available (Workflow/notebook)
          - SQL chunks (fallback) in Apps or test context
        """
        if not hypotheses:
            return
        if self._has_spark():
            self.write_hypotheses_spark(hypotheses)
        else:
            self.write_hypotheses_sql(hypotheses)

    # ------------------------------------------------------------------
    # MERGE (upsert) — for updating fitness scores after evaluation
    # ------------------------------------------------------------------

    def update_fitness_scores(self, hypotheses: list["Hypothesis"]):
        """
        Upsert fitness scores for already-written hypotheses.
        Used when evaluation completes asynchronously after initial write.
        """
        if not hypotheses:
            return

        if self._has_spark():
            spark = self._get_spark()
            updates = spark.createDataFrame([
                {
                    "id": h.id,
                    "fitness_statistical":  h.fitness_statistical,
                    "fitness_perplexity":   h.fitness_perplexity,
                    "fitness_semantic":     h.fitness_semantic,
                    "fitness_consistency":  h.fitness_consistency,
                    "fitness_adversarial":  h.fitness_adversarial,
                    "fitness_composite":    h.composite_fitness(),
                    "agent_eval_historian": h.agent_eval_historian,
                    "agent_eval_critic":    h.agent_eval_critic,
                    "decoded_sample":       h.decoded_sample[:500],
                    "mlflow_run_id":        h.mlflow_run_id,
                    "flagged_for_review":   h.flagged_for_review,
                }
                for h in hypotheses
            ])
            updates.createOrReplaceTempView("_fitness_updates")
            spark.sql(f"""
                MERGE INTO {self.config.population_table} AS target
                USING _fitness_updates AS src
                ON target.id = src.id
                WHEN MATCHED THEN UPDATE SET
                    target.fitness_statistical  = src.fitness_statistical,
                    target.fitness_perplexity   = src.fitness_perplexity,
                    target.fitness_semantic     = src.fitness_semantic,
                    target.fitness_consistency  = src.fitness_consistency,
                    target.fitness_adversarial  = src.fitness_adversarial,
                    target.fitness_composite    = src.fitness_composite,
                    target.agent_eval_historian = src.agent_eval_historian,
                    target.agent_eval_critic    = src.agent_eval_critic,
                    target.decoded_sample       = src.decoded_sample,
                    target.mlflow_run_id        = src.mlflow_run_id,
                    target.flagged_for_review   = src.flagged_for_review
            """)
        else:
            # SQL fallback: individual UPDATEs (slow but correct)
            for h in hypotheses:
                self._sql_exec(f"""
                    UPDATE {self.config.population_table}
                    SET
                        fitness_statistical  = {h.fitness_statistical:.6f},
                        fitness_perplexity   = {h.fitness_perplexity:.6f},
                        fitness_semantic     = {h.fitness_semantic:.6f},
                        fitness_consistency  = {h.fitness_consistency:.6f},
                        fitness_adversarial  = {h.fitness_adversarial:.6f},
                        fitness_composite    = {h.composite_fitness():.6f},
                        agent_eval_historian = {h.agent_eval_historian:.6f},
                        agent_eval_critic    = {h.agent_eval_critic:.6f},
                        decoded_sample       = '{h.decoded_sample[:500].replace(chr(39), chr(34))}',
                        mlflow_run_id        = '{h.mlflow_run_id}',
                        flagged_for_review   = {'true' if h.flagged_for_review else 'false'}
                    WHERE id = '{h.id}'
                """)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def load_generation(self, generation: int) -> list["Hypothesis"]:
        from .loop_agent import Hypothesis
        rows = self._sql_exec(
            f"SELECT * FROM {self.config.population_table} WHERE generation = {generation}"
        )
        return [Hypothesis.from_dict(r) for r in rows]

    def load_pareto_survivors(self, generation: int, top_n: int) -> list["Hypothesis"]:
        """Load top-N by composite fitness from a generation."""
        from .loop_agent import Hypothesis
        rows = self._sql_exec(f"""
            SELECT * FROM {self.config.population_table}
            WHERE generation = {generation}
            ORDER BY fitness_composite DESC
            LIMIT {top_n}
        """)
        return [Hypothesis.from_dict(r) for r in rows]

    def get_best_fitness_history(self, n_generations: int) -> list[float]:
        rows = self._sql_exec(f"""
            SELECT MAX(fitness_composite) as best
            FROM {self.config.population_table}
            GROUP BY generation
            ORDER BY generation DESC
            LIMIT {n_generations}
        """)
        return [float(r["best"]) for r in rows]

    def get_active_constraints(self) -> list[dict]:
        try:
            rows = self._sql_exec(
                "SELECT * FROM voynich.evolution.constraints WHERE active = true ORDER BY created_at DESC"
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # SQL execution helper
    # ------------------------------------------------------------------

    def _sql_exec(self, sql: str) -> list[dict]:
        from databricks.sdk.service.sql import StatementState
        resp = self.ws.statement_execution.execute_statement(
            warehouse_id=self.config.warehouse_id,
            statement=sql.strip(),
            wait_timeout="60s",
        )
        if resp.status.state == StatementState.FAILED:
            raise RuntimeError(
                f"SQL failed [{resp.status.error.error_code}]: {resp.status.error.message}\n"
                f"SQL: {sql[:200]}"
            )
        if not resp.result or not resp.result.data_array:
            return []
        cols = [c.name for c in resp.manifest.schema.columns]
        return [dict(zip(cols, row)) for row in resp.result.data_array]
