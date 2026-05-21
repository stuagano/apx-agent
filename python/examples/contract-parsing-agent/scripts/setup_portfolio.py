# Databricks notebook source
# MAGIC %md
# MAGIC # Setup portfolio for chatbot-contracts demo
# MAGIC
# MAGIC One-shot. Run after `provision_uc.py`. Outputs:
# MAGIC - PDFs in raw_contracts volume
# MAGIC - silver.contracts (extracted fields)
# MAGIC - silver.contracts_ground_truth (gold)
# MAGIC - MLflow run with per-field accuracy

# COMMAND ----------

# MAGIC %pip install --upgrade pymupdf reportlab pyyaml mlflow databricks-sdk
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import json
import sys
from pathlib import Path

# Resolved in Step 1 above. Replace this string with the path you confirmed.
REPO_ROOT = Path("/Workspace/Repos/<email-or-username>/<repo-name>/contract-parsing-agent")
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from databricks.sdk import WorkspaceClient

from contract_parsing_agent.backend.config import load_settings
from contract_parsing_agent.backend.extraction import extract
from scripts.generate_synthetic_contracts import generate

# COMMAND ----------

settings = load_settings(REPO_ROOT / "agent.config.yaml")
ws = WorkspaceClient()
print(f"Catalog: {settings.catalog}.{settings.schema}")
print(f"Model:   {settings.model}")

# COMMAND ----------

# Generate corpus into the volume.
raw_dir = Path(settings.volumes.raw)
raw_dir.mkdir(parents=True, exist_ok=True)
gt_rows = generate(raw_dir, n=18)
print(f"generated {len(gt_rows)} contracts in {raw_dir}")

# COMMAND ----------

# Extract each contract via FM API.
extracted: list[dict] = []
for row in gt_rows:
    pdf = raw_dir / f"{row['contract_id']}.pdf"
    try:
        result = extract(pdf, settings.extraction_schema, settings.model, ws)
        result["contract_id"] = row["contract_id"]
        extracted.append(result)
    except Exception as e:
        print(f"FAILED {row['contract_id']}: {e}")
        extracted.append({"contract_id": row["contract_id"], "_extraction_error": str(e)})

print(f"extracted {len(extracted)} (success: {sum(1 for r in extracted if '_extraction_error' not in r)})")

# COMMAND ----------

import pyspark.sql.functions as F  # noqa: E402

extracted_df = spark.createDataFrame([
    {k: v for k, v in r.items() if not k.startswith("_") and not isinstance(v, list)}
    for r in extracted
])
gt_df = spark.createDataFrame(gt_rows)

(extracted_df.write
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(settings.qualified_table("primary")))
(gt_df.write
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(settings.qualified_table("ground_truth")))

display(spark.table(settings.qualified_table("primary")))

# COMMAND ----------

# MLflow eval run.
import mlflow  # noqa: E402

mlflow.set_experiment("/Shared/contract-parsing-agent/eval")

joined = (
    spark.table(settings.qualified_table("primary")).alias("p")
        .join(spark.table(settings.qualified_table("ground_truth")).alias("g"),
              on="contract_id", how="inner")
).cache()

CATEGORICAL_FIELDS = ["counterparty", "contract_type", "pricing_model", "auto_renewal"]
# pricing_summary is free-text; score as non-empty (extraction present, not empty string)
NONEMPTY_FIELDS = ["pricing_summary"]
NUMERIC_FIELDS = ["term_years", "sla_uptime_pct"]
DATE_FIELDS = ["effective_date", "expiry_date"]

with mlflow.start_run(run_name="portfolio-eval") as run:
    metrics: dict[str, float] = {}
    mismatches = []
    total = joined.count()

    for f in CATEGORICAL_FIELDS:
        match = joined.filter(F.col(f"p.{f}") == F.col(f"g.{f}")).count()
        metrics[f"accuracy_{f}"] = match / total if total else 0.0
        rows = joined.filter(F.col(f"p.{f}") != F.col(f"g.{f}")).select(
            "contract_id", F.lit(f).alias("field"),
            F.col(f"p.{f}").alias("predicted"), F.col(f"g.{f}").alias("actual")
        ).collect()
        mismatches.extend([r.asDict() for r in rows])

    for f in NUMERIC_FIELDS:
        match = joined.filter(F.abs(F.col(f"p.{f}") - F.col(f"g.{f}")) < 0.5).count()
        metrics[f"accuracy_{f}"] = match / total if total else 0.0

    for f in DATE_FIELDS:
        # Allow up to 2 days drift from generator -> extraction -> string compare.
        match = joined.filter(F.datediff(F.col(f"p.{f}"), F.col(f"g.{f}")).between(-2, 2)).count()
        metrics[f"accuracy_{f}"] = match / total if total else 0.0

    for f in NONEMPTY_FIELDS:
        match = joined.filter(
            F.col(f"p.{f}").isNotNull() & (F.length(F.col(f"p.{f}")) > 10)
        ).count()
        metrics[f"accuracy_{f}"] = match / total if total else 0.0

    overall = sum(metrics.values()) / len(metrics) if metrics else 0.0
    metrics["overall_accuracy"] = overall

    for k, v in metrics.items():
        mlflow.log_metric(k, v)
    mlflow.log_param("model", settings.model)
    mlflow.log_param("contracts_count", total)

    mismatch_path = "/tmp/mismatches.json"
    Path(mismatch_path).write_text(json.dumps(mismatches, indent=2, default=str))
    mlflow.log_artifact(mismatch_path)

    print(f"overall_accuracy = {overall:.2%}")
    print(f"per-field: {metrics}")
    print(f"run_id = {run.info.run_id}")

# COMMAND ----------

# Pass-bar gate (matches the spec)
assert all(v >= 0.75 for k, v in metrics.items() if k.startswith("accuracy_")), \
    f"At least one field below 75%; iterate on prompt/schema before demoing. metrics={metrics}"
assert metrics["overall_accuracy"] >= 0.90, \
    f"Overall accuracy {metrics['overall_accuracy']:.2%} below 90% pass bar."
print("PASS BAR MET — demo-ready.")
