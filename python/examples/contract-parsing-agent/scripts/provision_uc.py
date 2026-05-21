# Databricks notebook source
# MAGIC %md
# MAGIC # Provision Unity Catalog resources for chatbot-contracts
# MAGIC
# MAGIC Run this once before `setup_portfolio.py`. Idempotent.

# COMMAND ----------

CATALOG = "<your-catalog>"  # Replace with your Unity Catalog catalog name
SCHEMA = "silver"
VOLUMES = ["raw_contracts", "uploads"]

# COMMAND ----------

spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
for v in VOLUMES:
    spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.{v}")

# COMMAND ----------

display(spark.sql(f"SHOW VOLUMES IN {CATALOG}.{SCHEMA}"))
