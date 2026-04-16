# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Human Review Gate
# MAGIC
# MAGIC Pauses between generation batches to surface top candidates for expert review.
# MAGIC Called by the Workflow between generation batch 1 and batch 2.
# MAGIC
# MAGIC **This notebook does NOT auto-proceed.**
# MAGIC A researcher must review the queue in the Databricks Apps UI and then
# MAGIC manually approve continuation by running the final cell.

# COMMAND ----------

# MAGIC %pip install apx-agent>=0.16.0

# COMMAND ----------

import os
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from databricks.sdk import WorkspaceClient

spark = SparkSession.builder.getOrCreate()
ws    = WorkspaceClient()

REVIEW_URL = os.getenv("REVIEW_URL", "")  # Passed as Workflow parameter

# COMMAND ----------

# MAGIC %md ## Current population summary

# COMMAND ----------

summary = spark.sql("""
    SELECT
        generation,
        COUNT(*) as population_size,
        ROUND(MAX(fitness_composite), 4) as best_fitness,
        ROUND(AVG(fitness_composite), 4) as avg_fitness,
        SUM(CASE WHEN flagged_for_review THEN 1 ELSE 0 END) as flagged
    FROM voynich.evolution.population
    GROUP BY generation
    ORDER BY generation DESC
    LIMIT 10
""")

print("=== Generation Summary (last 10) ===")
display(summary)

# COMMAND ----------

# MAGIC %md ## Top candidates awaiting review

# COMMAND ----------

review_queue = spark.sql("""
    SELECT
        r.hypothesis_id,
        r.expert_type,
        r.reason,
        r.flagged_at,
        p.fitness_composite,
        p.cipher_type,
        p.source_language,
        p.agent_eval_historian,
        p.agent_eval_critic,
        SUBSTR(p.decoded_sample, 1, 200) as decoded_preview
    FROM voynich.evolution.review_queue r
    JOIN voynich.evolution.population p ON r.hypothesis_id = p.id
    WHERE r.status = 'pending'
    ORDER BY p.fitness_composite DESC
""")

count = review_queue.count()
print(f"=== Review Queue: {count} candidates pending ===")
if count > 0:
    display(review_queue)

# COMMAND ----------

# MAGIC %md ## Agent health check

# COMMAND ----------

agent_health = spark.sql("""
    SELECT
        agent_name,
        COUNT(*) as eval_count,
        ROUND(AVG(composite_eval_score), 4) as avg_score,
        ROUND(MIN(composite_eval_score), 4) as min_score,
        SUM(CASE WHEN action_triggered != 'OK' THEN 1 ELSE 0 END) as issues
    FROM voynich.evolution.agent_evals
    GROUP BY agent_name
    ORDER BY avg_score ASC
""")

print("=== Agent Eval Health ===")
display(agent_health)

poor_agents = [
    row.agent_name for row in agent_health.collect()
    if row.avg_score < 0.5
]
if poor_agents:
    print(f"\n⚠️  FLAGGED FOR PROMPT REFINEMENT: {poor_agents}")
    print("Review agent reasoning traces in MLflow Tracking before continuing.")

# COMMAND ----------

# MAGIC %md ## Active constraints

# COMMAND ----------

constraints = spark.sql("""
    SELECT constraint_type, constraint_value, target_section, created_by, created_at
    FROM voynich.evolution.constraints
    WHERE active = true
    ORDER BY created_at DESC
""")
count = constraints.count()
print(f"=== Active Constraints: {count} ===")
if count > 0:
    display(constraints)
else:
    print("No active constraints. Researchers can add constraints via the Apps UI.")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## ✋ MANUAL REVIEW REQUIRED
# MAGIC
# MAGIC Before running the next cell:
# MAGIC
# MAGIC 1. Open the Orchestrator App:
# MAGIC    ```
# MAGIC    {review_url}
# MAGIC    ```
# MAGIC 2. Review flagged candidates in the queue
# MAGIC 3. Annotate promising ones with domain expertise
# MAGIC 4. Inject any constraints (e.g. "force herbal section to Latin")
# MAGIC 5. Flag poor-scoring agents for prompt refinement if needed
# MAGIC
# MAGIC **Run the next cell ONLY when you're ready to continue the loop.**

# COMMAND ----------

# Approval gate — this cell must be run manually to unblock the Workflow
# In automated runs, the Workflow pauses here waiting for the job to complete

APPROVED = True  # Researcher changes this to True and runs the cell

if not APPROVED:
    raise Exception(
        "Review gate not approved. Set APPROVED = True and re-run this cell to continue."
    )

# Log the approval
spark.sql(f"""
    INSERT INTO voynich.evolution.constraints
    (constraint_type, constraint_value, target_section, active, created_by)
    VALUES ('review_gate', 'approved', 'all', false, 'researcher_approval')
""")

print("✓ Review gate approved — continuing to generation batch 2")

# COMMAND ----------

# MAGIC %md ## Final stats before batch 2

# COMMAND ----------

best = spark.sql("""
    SELECT *
    FROM voynich.evolution.population
    ORDER BY fitness_composite DESC
    LIMIT 3
""")

print("=== Top 3 candidates entering batch 2 ===")
display(best.select(
    "id", "generation", "cipher_type", "source_language",
    "fitness_composite", "agent_eval_historian", "agent_eval_critic",
    F.substring("decoded_sample", 1, 300).alias("decoded_preview")
))
