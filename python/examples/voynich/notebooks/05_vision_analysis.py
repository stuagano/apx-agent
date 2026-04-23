# Databricks notebook source

# COMMAND ----------

# MAGIC %md # Vision Analysis of Voynich Herbal Folios
# MAGIC
# MAGIC Sends each herbal folio image to FMAPI (Claude Sonnet 4.6) for structured
# MAGIC visual analysis. Results are written to `folio_vision_analysis` Delta table
# MAGIC for use by the voynich-grounder scoring agent.

# COMMAND ----------

import json
import base64
import time
import requests
from datetime import datetime

# COMMAND ----------

# MAGIC %md ### Configuration

# COMMAND ----------

VOLUME_PATH = "/Volumes/serverless_stable_qh44kx_catalog/voynich/images/herbal"
CATALOG_SCHEMA = "serverless_stable_qh44kx_catalog.voynich"
TABLE_NAME = f"{CATALOG_SCHEMA}.folio_vision_analysis"
MODEL = "databricks-claude-sonnet-4-6"
PROMPT_VERSION = "v1"

# FMAPI endpoint
HOST = spark.conf.get("spark.databricks.workspaceUrl", "")
if not HOST.startswith("https://"):
    HOST = f"https://{HOST}"

# COMMAND ----------

# MAGIC %md ### Create table if not exists

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
  folio_id            STRING,
  section             STRING,
  image_url           STRING,
  volume_path         STRING,
  subject_candidates  STRING,
  spatial_layout      STRING,
  visual_description  STRING,
  botanical_features  STRING,
  expected_terms      STRING,
  model_id            STRING,
  analyzed_at         TIMESTAMP,
  prompt_version      STRING
)
""")
print(f"Table {TABLE_NAME} ready")

# COMMAND ----------

# MAGIC %md ### Load folio manifest

# COMMAND ----------

manifest_path = f"{VOLUME_PATH}/manifest.json"
with open(manifest_path) as f:
    folio_manifest = json.load(f)

# Check which folios are already analyzed at this prompt version
existing = set()
try:
    rows = spark.sql(f"SELECT folio_id FROM {TABLE_NAME} WHERE prompt_version = '{PROMPT_VERSION}'").collect()
    existing = {r.folio_id for r in rows}
except Exception:
    pass

to_analyze = [fi for fi in folio_manifest if fi["folio_id"] not in existing]
print(f"{len(folio_manifest)} folios in manifest, {len(existing)} already analyzed, {len(to_analyze)} to process")

# COMMAND ----------

# MAGIC %md ### Vision analysis prompt

# COMMAND ----------

VISION_PROMPT = """You are analyzing a folio from the Voynich Manuscript (MS 408), a 15th-century document of unknown origin. This folio is from the herbal section.

Analyze this image and return a JSON object with exactly these fields:

1. "subject_candidates": Array of possible identifications for the plant depicted.
   For each: {"name": "common name", "latin": "Latin binomial if known",
   "confidence": 0.0-1.0, "reasoning": "brief visual evidence"}
   Include 1-3 candidates ranked by confidence.

2. "spatial_layout": {"text_regions": [...]} where each region has:
   {"position": "top-left|top-right|bottom|left|right|interlinear",
    "role": "label|description|recipe|marginalia|unknown",
    "estimated_lines": number}

3. "visual_description": A 2-3 sentence description of the plant's visual
   features (leaf shape, root structure, flowers, colors, scale).

4. "botanical_features": ["serrated leaves", "thick taproot", "red berries", ...]
   Observable morphological features useful for identification.

Return ONLY the JSON object, no markdown."""

# COMMAND ----------

# MAGIC %md ### Run vision analysis

# COMMAND ----------

# Get notebook token for FMAPI
token = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()

results = []
errors = []
for i, fi in enumerate(to_analyze):
    folio_id = fi["folio_id"]
    image_path = fi["volume_path"]

    print(f"[{i+1}/{len(to_analyze)}] Analyzing {folio_id}...")

    try:
        with open(image_path, "rb") as img_file:
            image_b64 = base64.b64encode(img_file.read()).decode("utf-8")

        payload = {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}",
                            },
                        },
                        {
                            "type": "text",
                            "text": VISION_PROMPT,
                        },
                    ],
                }
            ],
            "max_tokens": 2048,
        }

        resp = requests.post(
            f"{HOST}/serving-endpoints/{MODEL}/invocations",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract the text response
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = content[0].get("text", "")

        # Parse JSON from response (strip markdown fences if present)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        analysis = json.loads(content)

        result = {
            "folio_id": folio_id,
            "section": fi["section"],
            "image_url": fi.get("iiif_url", ""),
            "volume_path": image_path,
            "subject_candidates": json.dumps(analysis.get("subject_candidates", [])),
            "spatial_layout": json.dumps(analysis.get("spatial_layout", {})),
            "visual_description": analysis.get("visual_description", ""),
            "botanical_features": json.dumps(analysis.get("botanical_features", [])),
            "expected_terms": "{}",
            "model_id": MODEL,
            "analyzed_at": datetime.utcnow().isoformat(),
            "prompt_version": PROMPT_VERSION,
        }
        results.append(result)
        print(f"  OK: {len(analysis.get('subject_candidates', []))} candidates")

    except Exception as e:
        errors.append((folio_id, str(e)))
        print(f"  ERROR: {e}")

    time.sleep(1)  # rate limit

print(f"\n{len(results)} analyzed, {len(errors)} errors")

# COMMAND ----------

# MAGIC %md ### Write to Delta table

# COMMAND ----------

if results:
    df = spark.createDataFrame(results)
    df.write.mode("append").saveAsTable(TABLE_NAME)
    print(f"Wrote {len(results)} rows to {TABLE_NAME}")

if errors:
    print(f"\nErrors ({len(errors)}):")
    for fid, err in errors:
        print(f"  {fid}: {err}")

# COMMAND ----------

# MAGIC %md ### Verify

# COMMAND ----------

display(spark.sql(f"""
SELECT folio_id, visual_description,
       get_json_object(subject_candidates, '$[0].name') AS top_candidate,
       get_json_object(subject_candidates, '$[0].confidence') AS confidence
FROM {TABLE_NAME}
WHERE prompt_version = '{PROMPT_VERSION}'
ORDER BY folio_id
"""))
