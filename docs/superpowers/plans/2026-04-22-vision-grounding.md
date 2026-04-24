# Vision Grounding System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add image-based grounding to the Voynich evolutionary loop so fitness scoring uses real visual signal instead of LLM-hallucinated metrics.

**Architecture:** Batch vision analysis of ~130 herbal folios via FMAPI (one-time), cached to a Delta table. New `voynich-grounder` agent reads the cache and scores hypotheses by text overlap with expected botanical terms. Grounder slots into the existing orchestrator as an additional fitness agent.

**Tech Stack:** TypeScript (Express + apx-agent), Python (notebooks for image download + vision analysis), Databricks SQL, FMAPI (databricks-claude-sonnet-4-6), IIIF image API.

---

## File Structure

### New Files
- `python/examples/voynich/notebooks/04_download_folio_images.py` — Downloads herbal folio images from Beinecke IIIF to UC Volume
- `python/examples/voynich/notebooks/05_vision_analysis.py` — Runs FMAPI vision over each image, writes to `folio_vision_analysis`
- `typescript/examples/voynich/grounder/app.ts` — Grounder agent with `score_image_grounding` tool

### Modified Files
- `typescript/examples/voynich/voynich-config.ts` — Add grounding weight, Pareto objective, folio manifest
- `typescript/scripts/build-deploy.sh` — Add grounder to the build/deploy loop

---

### Task 1: Folio Manifest + Updated Config

**Files:**
- Modify: `typescript/examples/voynich/voynich-config.ts`

- [ ] **Step 1: Add herbal folio manifest to voynich-config.ts**

The Beinecke Digital Library serves Voynich folios via IIIF. The herbal section folios f1 through f20 each have recto (r) and verso (v) sides. Add the manifest and updated fitness weights:

```ts
// After SECTION_TO_INDEX, add:

// ---------------------------------------------------------------------------
// Herbal folio manifest (Phase 1 grounding)
// ---------------------------------------------------------------------------

/**
 * Herbal section folios with Beinecke IIIF image URLs.
 * Each folio has recto (r) and/or verso (v) sides with plant illustrations.
 * IIIF URL pattern: https://collections.library.yale.edu/iiif/2/{asset_id}/full/1024,/0/default.jpg
 *
 * Asset IDs sourced from the Beinecke Digital Library (MS 408).
 */
export const HERBAL_FOLIOS: Array<{ folio_id: string; section: string; iiif_url: string }> = [
  { folio_id: 'f1r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006065/full/1024,/0/default.jpg' },
  { folio_id: 'f1v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006066/full/1024,/0/default.jpg' },
  { folio_id: 'f2r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006067/full/1024,/0/default.jpg' },
  { folio_id: 'f2v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006068/full/1024,/0/default.jpg' },
  { folio_id: 'f3r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006069/full/1024,/0/default.jpg' },
  { folio_id: 'f3v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006070/full/1024,/0/default.jpg' },
  { folio_id: 'f4r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006071/full/1024,/0/default.jpg' },
  { folio_id: 'f4v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006072/full/1024,/0/default.jpg' },
  { folio_id: 'f5r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006073/full/1024,/0/default.jpg' },
  { folio_id: 'f5v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006074/full/1024,/0/default.jpg' },
  { folio_id: 'f6r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006075/full/1024,/0/default.jpg' },
  { folio_id: 'f6v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006076/full/1024,/0/default.jpg' },
  { folio_id: 'f7r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006077/full/1024,/0/default.jpg' },
  { folio_id: 'f7v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006078/full/1024,/0/default.jpg' },
  { folio_id: 'f8r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006079/full/1024,/0/default.jpg' },
  { folio_id: 'f8v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006080/full/1024,/0/default.jpg' },
  { folio_id: 'f9r',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006081/full/1024,/0/default.jpg' },
  { folio_id: 'f9v',  section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006082/full/1024,/0/default.jpg' },
  { folio_id: 'f10r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006083/full/1024,/0/default.jpg' },
  { folio_id: 'f10v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006084/full/1024,/0/default.jpg' },
  { folio_id: 'f11r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006085/full/1024,/0/default.jpg' },
  { folio_id: 'f11v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006086/full/1024,/0/default.jpg' },
  { folio_id: 'f12r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006087/full/1024,/0/default.jpg' },
  { folio_id: 'f12v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006088/full/1024,/0/default.jpg' },
  { folio_id: 'f13r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006089/full/1024,/0/default.jpg' },
  { folio_id: 'f13v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006090/full/1024,/0/default.jpg' },
  { folio_id: 'f14r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006091/full/1024,/0/default.jpg' },
  { folio_id: 'f14v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006092/full/1024,/0/default.jpg' },
  { folio_id: 'f15r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006093/full/1024,/0/default.jpg' },
  { folio_id: 'f15v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006094/full/1024,/0/default.jpg' },
  { folio_id: 'f16r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006095/full/1024,/0/default.jpg' },
  { folio_id: 'f16v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006096/full/1024,/0/default.jpg' },
  { folio_id: 'f17r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006097/full/1024,/0/default.jpg' },
  { folio_id: 'f17v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006098/full/1024,/0/default.jpg' },
  { folio_id: 'f18r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006099/full/1024,/0/default.jpg' },
  { folio_id: 'f18v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006100/full/1024,/0/default.jpg' },
  { folio_id: 'f19r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006101/full/1024,/0/default.jpg' },
  { folio_id: 'f19v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006102/full/1024,/0/default.jpg' },
  { folio_id: 'f20r', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006103/full/1024,/0/default.jpg' },
  { folio_id: 'f20v', section: 'herbal', iiif_url: 'https://collections.library.yale.edu/iiif/2/1006104/full/1024,/0/default.jpg' },
];
```

Note: The IIIF asset IDs above are placeholders — Task 2 will discover the real IDs from the Beinecke IIIF manifest. The URL pattern is correct.

- [ ] **Step 2: Update fitness weights**

Replace the existing `VOYNICH_FITNESS_WEIGHTS` and `VOYNICH_PARETO_OBJECTIVES`:

```ts
export const VOYNICH_FITNESS_WEIGHTS: Record<string, number> = {
  semantic: 0.15,
  perplexity: 0.10,
  consistency: 0.05,
  statistical: 0.10,
  adversarial: 0.10,
  grounding: 0.50,
};

export const VOYNICH_PARETO_OBJECTIVES: string[] = [
  'grounding',
  'semantic',
  'statistical',
  'perplexity',
];
```

- [ ] **Step 3: Commit**

```bash
git add typescript/examples/voynich/voynich-config.ts
git commit -m "feat(voynich): add herbal folio manifest + grounding fitness weight"
```

---

### Task 2: Image Download Notebook

**Files:**
- Create: `python/examples/voynich/notebooks/04_download_folio_images.py`

This Databricks notebook discovers the real IIIF asset IDs from the Beinecke manifest, downloads herbal folio images, and saves them to a UC Volume.

- [ ] **Step 1: Write the download notebook**

```python
# Databricks notebook source

# COMMAND ----------

# MAGIC %md # Download Voynich Herbal Folio Images
# MAGIC
# MAGIC Downloads herbal section folio images from the Beinecke Rare Book & Manuscript Library
# MAGIC IIIF endpoint and saves to a UC Volume for vision analysis.

# COMMAND ----------

import requests
import json
import time
import os

# COMMAND ----------

# MAGIC %md ### Configuration

# COMMAND ----------

VOLUME_PATH = "/Volumes/serverless_stable_qh44kx_catalog/voynich/images/herbal"
IIIF_MANIFEST_URL = "https://collections.library.yale.edu/manifests/2002046"
IMAGE_WIDTH = 1024  # px — sufficient for plant identification

# Herbal section folio identifiers (f1 through f20, recto and verso)
HERBAL_FOLIO_PREFIXES = [f"f{i}" for i in range(1, 21)]

# COMMAND ----------

# MAGIC %md ### Fetch IIIF Manifest
# MAGIC
# MAGIC The Beinecke IIIF manifest lists all canvases (pages) with their image service URLs.
# MAGIC We filter for herbal section folios.

# COMMAND ----------

print("Fetching IIIF manifest...")
resp = requests.get(IIIF_MANIFEST_URL, timeout=30)
resp.raise_for_status()
manifest = resp.json()

# Extract canvases — each canvas is one folio page
canvases = manifest.get("sequences", [{}])[0].get("canvases", [])
# IIIF v3 fallback
if not canvases:
    canvases = manifest.get("items", [])

print(f"Found {len(canvases)} canvases in manifest")

# COMMAND ----------

# MAGIC %md ### Map Canvases to Folio IDs

# COMMAND ----------

import re

folio_images = []
for canvas in canvases:
    label = canvas.get("label", "")
    if isinstance(label, dict):
        label = list(label.values())[0][0] if label else ""

    # Match folio labels like "f1r", "f1v", "fol. 1r", etc.
    match = re.search(r'(?:fol\.?\s*)?(\d+)\s*([rv])', str(label), re.IGNORECASE)
    if not match:
        continue

    folio_num = int(match.group(1))
    side = match.group(2).lower()
    folio_id = f"f{folio_num}{side}"
    base = f"f{folio_num}"

    if base not in HERBAL_FOLIO_PREFIXES:
        continue

    # Extract image service URL
    images = canvas.get("images", [])
    if not images:
        # IIIF v3
        items = canvas.get("items", [{}])
        for item in items:
            for anno in item.get("items", []):
                body = anno.get("body", {})
                if body.get("type") == "Image":
                    images.append({"resource": body})

    if not images:
        continue

    resource = images[0].get("resource", {})
    service = resource.get("service", {})
    if isinstance(service, list):
        service = service[0] if service else {}

    service_id = service.get("@id") or service.get("id", "")
    if service_id:
        image_url = f"{service_id}/full/{IMAGE_WIDTH},/0/default.jpg"
    else:
        image_url = resource.get("@id", "")

    if image_url:
        folio_images.append({
            "folio_id": folio_id,
            "section": "herbal",
            "iiif_url": image_url,
        })

print(f"Matched {len(folio_images)} herbal folio images")
for fi in folio_images[:5]:
    print(f"  {fi['folio_id']}: {fi['iiif_url'][:80]}...")

# COMMAND ----------

# MAGIC %md ### Download Images to UC Volume

# COMMAND ----------

# Create volume directory if needed
os.makedirs(VOLUME_PATH, exist_ok=True)

downloaded = 0
errors = []
for fi in folio_images:
    dest = f"{VOLUME_PATH}/{fi['folio_id']}.jpg"
    if os.path.exists(dest):
        print(f"  {fi['folio_id']}: already exists, skipping")
        downloaded += 1
        continue

    try:
        r = requests.get(fi["iiif_url"], timeout=60)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
        fi["volume_path"] = dest
        downloaded += 1
        print(f"  {fi['folio_id']}: downloaded ({len(r.content)} bytes)")
        time.sleep(0.5)  # be polite to the IIIF server
    except Exception as e:
        errors.append((fi["folio_id"], str(e)))
        print(f"  {fi['folio_id']}: ERROR - {e}")

print(f"\nDownloaded {downloaded}/{len(folio_images)} images, {len(errors)} errors")

# COMMAND ----------

# MAGIC %md ### Save manifest for vision analysis notebook

# COMMAND ----------

# Update volume_path for all entries
for fi in folio_images:
    fi["volume_path"] = f"{VOLUME_PATH}/{fi['folio_id']}.jpg"

manifest_path = f"{VOLUME_PATH}/manifest.json"
with open(manifest_path, "w") as f:
    json.dump(folio_images, f, indent=2)

print(f"Manifest written to {manifest_path} ({len(folio_images)} entries)")
```

- [ ] **Step 2: Commit**

```bash
git add python/examples/voynich/notebooks/04_download_folio_images.py
git commit -m "feat(voynich): notebook to download herbal folio images from Beinecke IIIF"
```

---

### Task 3: Vision Analysis Notebook

**Files:**
- Create: `python/examples/voynich/notebooks/05_vision_analysis.py`

This Databricks notebook reads downloaded images, sends each to FMAPI with a structured vision prompt, and writes results to the `folio_vision_analysis` Delta table.

- [ ] **Step 1: Write the vision analysis notebook**

```python
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

# FMAPI endpoint — uses workspace token from notebook context
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
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
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
            "image_url": fi["iiif_url"],
            "volume_path": image_path,
            "subject_candidates": json.dumps(analysis.get("subject_candidates", [])),
            "spatial_layout": json.dumps(analysis.get("spatial_layout", {})),
            "visual_description": analysis.get("visual_description", ""),
            "botanical_features": json.dumps(analysis.get("botanical_features", [])),
            "expected_terms": "{}",  # populated lazily by grounder
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
```

- [ ] **Step 2: Commit**

```bash
git add python/examples/voynich/notebooks/05_vision_analysis.py
git commit -m "feat(voynich): vision analysis notebook for herbal folios via FMAPI"
```

---

### Task 4: Grounder Agent

**Files:**
- Create: `typescript/examples/voynich/grounder/app.ts`

The grounder agent follows the same pattern as historian/critic: Express app with apx-agent plugin, one tool, JSON-only LLM output.

- [ ] **Step 1: Write the grounder agent**

```ts
/**
 * Voynich Grounder — image grounding fitness scorer for the Voynich Manuscript.
 *
 * Scores how well a decoded text passage matches what is depicted in the
 * associated folio illustration. Reads pre-cached vision analysis from the
 * folio_vision_analysis Delta table (no vision API calls in the hot loop).
 *
 * Required environment variables:
 *   DATABRICKS_HOST          Workspace URL
 *   DATABRICKS_WAREHOUSE_ID  SQL warehouse for reading folio_vision_analysis
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.cloud.databricks.com \
 *   DATABRICKS_TOKEN=your-token \
 *   DATABRICKS_WAREHOUSE_ID=your-warehouse-id \
 *   npx tsx app.ts
 */

import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  createDevPlugin,
  defineTool,
  resolveToken,
  resolveHost,
} from '../../../src/index.js';
import { VOYNICH_SOURCE_LANGUAGES } from '../voynich-config.js';

// ---------------------------------------------------------------------------
// SQL helper
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<unknown[]> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');

  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      warehouse_id: warehouseId,
      statement,
      wait_timeout: '30s',
    }),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`SQL ${res.status}: ${text}`);
  }

  const data = (await res.json()) as {
    result?: { data_array?: string[][] };
    manifest?: { columns?: Array<{ name: string }> };
    status?: { state?: string; error?: { message?: string } };
  };

  if (data.status?.state === 'FAILED') {
    throw new Error(`SQL failed: ${data.status.error?.message}`);
  }

  const columns = data.manifest?.columns?.map((c) => c.name) ?? [];
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => {
    const obj: Record<string, string> = {};
    columns.forEach((col, i) => {
      obj[col] = row[i];
    });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// Vision analysis cache
// ---------------------------------------------------------------------------

const ANALYSIS_TABLE =
  process.env.VISION_TABLE ?? 'serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis';

interface FolioAnalysis {
  folio_id: string;
  subject_candidates: Array<{
    name: string;
    latin?: string;
    confidence: number;
    reasoning?: string;
  }>;
  spatial_layout: {
    text_regions: Array<{
      position: string;
      role: string;
      estimated_lines: number;
    }>;
  };
  visual_description: string;
  botanical_features: string[];
  expected_terms: Record<string, string[]>;
}

let analysisCache: FolioAnalysis[] | null = null;

async function loadAnalyses(): Promise<FolioAnalysis[]> {
  if (analysisCache) return analysisCache;

  const rows = (await executeSql(
    `SELECT folio_id, subject_candidates, spatial_layout, visual_description,
            botanical_features, expected_terms
     FROM ${ANALYSIS_TABLE}
     WHERE section = 'herbal'
     ORDER BY folio_id`,
  )) as Array<Record<string, string>>;

  analysisCache = rows.map((r) => ({
    folio_id: r.folio_id,
    subject_candidates: JSON.parse(r.subject_candidates || '[]'),
    spatial_layout: JSON.parse(r.spatial_layout || '{"text_regions":[]}'),
    visual_description: r.visual_description || '',
    botanical_features: JSON.parse(r.botanical_features || '[]'),
    expected_terms: JSON.parse(r.expected_terms || '{}'),
  }));

  return analysisCache;
}

// ---------------------------------------------------------------------------
// Scoring logic
// ---------------------------------------------------------------------------

function scoreOverlap(decodedText: string, expectedTerms: string[]): number {
  if (expectedTerms.length === 0) return 0;

  const decoded = decodedText.toLowerCase().replace(/[^a-z\s]/g, ' ');
  const tokens = decoded.split(/\s+/).filter((t) => t.length > 2);
  if (tokens.length === 0) return 0;

  let matchScore = 0;
  for (const term of expectedTerms) {
    const termLower = term.toLowerCase();

    // Exact token match
    if (tokens.includes(termLower)) {
      matchScore += 1.0;
      continue;
    }

    // Substring match (decoded contains the expected term)
    if (decoded.includes(termLower)) {
      matchScore += 0.7;
      continue;
    }

    // Partial stem match (first 4+ chars match)
    if (termLower.length >= 4) {
      const stem = termLower.slice(0, Math.min(termLower.length, 5));
      if (tokens.some((t) => t.startsWith(stem))) {
        matchScore += 0.4;
        continue;
      }
    }
  }

  // Normalize by number of expected terms, cap at 1.0
  return Math.min(1.0, matchScore / expectedTerms.length);
}

// ---------------------------------------------------------------------------
// Tool: score_image_grounding
// ---------------------------------------------------------------------------

const scoreImageGrounding = defineTool({
  name: 'score_image_grounding',
  description:
    'Score how well a decoded Voynich text passage matches what is depicted in ' +
    'the associated herbal folio illustrations. Reads cached vision analysis — ' +
    'no vision API calls. Returns a grounding score from 0 (no match) to 1 (strong match).',
  parameters: z.object({
    decoded_text: z
      .string()
      .describe('The decoded/translated text passage to evaluate.'),
    source_language: z
      .string()
      .describe('The candidate source language (e.g., latin, italian, hebrew).'),
    section: z
      .string()
      .default('herbal')
      .describe('Manuscript section (herbal for Phase 1).'),
    folio_id: z
      .string()
      .optional()
      .describe('Specific folio to score against, or omit to score against all herbal folios.'),
  }),
  handler: async ({
    decoded_text,
    source_language,
    section: _section,
    folio_id,
  }: {
    decoded_text: string;
    source_language: string;
    section?: string;
    folio_id?: string;
  }) => {
    const analyses = await loadAnalyses();
    const targets = folio_id
      ? analyses.filter((a) => a.folio_id === folio_id)
      : analyses;

    if (targets.length === 0) {
      return { grounding: 0, error: 'No folio analyses found' };
    }

    let bestScore = 0;
    let bestFolio = '';
    let bestDepicted = '';
    let bestMatched: string[] = [];

    for (const analysis of targets) {
      // Get or generate expected terms for this language
      let terms = analysis.expected_terms[source_language];
      if (!terms || terms.length === 0) {
        // Build expected terms from subject candidates + botanical features
        terms = [];
        for (const candidate of analysis.subject_candidates) {
          if (candidate.name) terms.push(candidate.name);
          if (candidate.latin) {
            terms.push(candidate.latin);
            // Add genus name alone (first word of binomial)
            const genus = candidate.latin.split(' ')[0];
            if (genus) terms.push(genus);
          }
        }
        terms.push(...analysis.botanical_features);
      }

      const score = scoreOverlap(decoded_text, terms);
      if (score > bestScore) {
        bestScore = score;
        bestFolio = analysis.folio_id;
        bestDepicted =
          analysis.subject_candidates[0]?.name ?? analysis.visual_description.slice(0, 50);
        bestMatched = terms.filter(
          (t) => decoded_text.toLowerCase().includes(t.toLowerCase()),
        );
      }
    }

    return {
      grounding: Math.round(bestScore * 1000) / 1000,
      best_folio: bestFolio,
      depicted: bestDepicted,
      matched_terms: bestMatched,
      folios_scored: targets.length,
    };
  },
});

// ---------------------------------------------------------------------------
// Agent plugin
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich Grounder, a visual grounding specialist for the Voynich Manuscript.',
    'Use the score_image_grounding tool to evaluate whether a decoded text passage',
    'matches what is depicted in the manuscript herbal illustrations.',
    '',
    'When you receive a hypothesis object, extract decoded_text and source_language,',
    'then call score_image_grounding and respond with ONLY a JSON object:',
    '  { "grounding": <0-1> }',
    '',
    'Do not add explanations. Respond with ONLY the JSON object.',
  ].join('\n'),
  tools: [scoreImageGrounding],
});

const agentExports = () => agentPlugin.exports();

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  {
    name: 'voynich-grounder',
    description: 'Image grounding fitness scorer for Voynich Manuscript decoded text',
  },
  agentExports,
);
discoveryPlugin.setup();

const mcpPlugin = createMcpPlugin({}, agentExports);
mcpPlugin.setup().catch(console.error);

const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
mcpPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const port = parseInt(process.env.PORT ?? '8004');
const server = app.listen(port, () => {
  console.log(`Voynich Grounder running at http://localhost:${port}`);
  console.log(`  /responses               — agent endpoint (Responses API)`);
  console.log(`  /.well-known/agent.json  — A2A discovery card`);
  console.log(`  /mcp                     — MCP server`);
  console.log(`  /_apx/agent              — dev chat UI`);
  console.log(`  /_apx/tools              — tool inspector`);
});
server.timeout = 180_000;
server.keepAliveTimeout = 90_000;
```

- [ ] **Step 2: Commit**

```bash
git add typescript/examples/voynich/grounder/app.ts
git commit -m "feat(voynich): grounder agent with score_image_grounding tool"
```

---

### Task 5: Add Grounder to Build/Deploy Script

**Files:**
- Modify: `typescript/scripts/build-deploy.sh`

- [ ] **Step 1: Add grounder to the AGENTS list and SP mapping**

In `build-deploy.sh`, update the `AGENTS` variable and `sp_client_id` function:

```bash
AGENTS="decipherer historian critic judge orchestrator grounder"
```

Add to `sp_client_id()`:
```bash
    grounder)     echo "PLACEHOLDER_CLIENT_ID" ;;
```

Add grounder-specific env vars in the orchestrator section — update the orchestrator's `FITNESS_AGENT_URLS` to include the grounder:

```bash
  # Orchestrator needs extra env vars
  if [ "$AGENT" = "orchestrator" ]; then
    cat >> "$DIR/app.yaml" <<EOF
  - name: MUTATION_AGENT_URL
    value: "https://voynich-decipherer-$APP_DOMAIN"
  - name: FITNESS_AGENT_URLS
    value: "https://voynich-historian-$APP_DOMAIN,https://voynich-critic-$APP_DOMAIN,https://voynich-grounder-$APP_DOMAIN"
  - name: JUDGE_AGENT_URL
    value: "https://voynich-judge-$APP_DOMAIN"
```

Add grounder-specific env vars:

```bash
  if [ "$AGENT" = "grounder" ]; then
    cat >> "$DIR/app.yaml" <<EOF
  - name: VISION_TABLE
    value: "serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis"
EOF
  fi
```

- [ ] **Step 2: Commit**

```bash
git add typescript/scripts/build-deploy.sh
git commit -m "feat(voynich): add grounder to build-deploy script"
```

---

### Task 6: Build, Deploy, and Verify

This task builds the grounder, creates the Databricks App, sets up the SP, and deploys.

- [ ] **Step 1: Build the framework**

```bash
cd typescript && npm run build
```

- [ ] **Step 2: Create the grounder app on Databricks**

```bash
databricks apps create voynich-grounder --profile fe-stable
```

- [ ] **Step 3: Create service principal for grounder**

Look up the SP that Databricks created for the app, generate a secret, and grant UC permissions:

```bash
# Get the SP application ID from the app
databricks apps get voynich-grounder --profile fe-stable -o json | python3 -c "import sys,json; print(json.load(sys.stdin).get('service_principal_client_id','?'))"

# Create a secret for the SP (use numeric ID from service-principals list)
databricks service-principal-secrets-proxy create <numeric_id> --profile fe-stable

# Grant UC permissions
databricks grants update catalog serverless_stable_qh44kx_catalog --profile fe-stable --json '{"changes":[{"principal":"<client_id>","add":["USE_CATALOG"]}]}'
databricks grants update schema serverless_stable_qh44kx_catalog.voynich --profile fe-stable --json '{"changes":[{"principal":"<client_id>","add":["USE_SCHEMA","SELECT","MODIFY"]}]}'
```

- [ ] **Step 4: Build deploy directory for grounder**

Manually run the relevant parts of `build-deploy.sh` for just the grounder, or run the full script with the SP secrets set.

- [ ] **Step 5: Upload and deploy**

```bash
databricks workspace import-dir deploy/voynich-grounder /Workspace/Users/stuart.gano@databricks.com/voynich-grounder --profile fe-stable --overwrite
databricks apps deploy voynich-grounder --source-code-path /Workspace/Users/stuart.gano@databricks.com/voynich-grounder --profile fe-stable
```

- [ ] **Step 6: Verify grounder is running**

```bash
databricks apps get voynich-grounder --profile fe-stable -o json | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'deploy: {d.get(\"active_deployment\",{}).get(\"status\",{}).get(\"state\",\"?\")}, app: {d.get(\"app_status\",{}).get(\"state\",\"?\")}')"
```

Expected: `deploy: SUCCEEDED, app: RUNNING`

- [ ] **Step 7: Update orchestrator FITNESS_AGENT_URLS and redeploy**

Add grounder URL to the orchestrator's app.yaml and redeploy orchestrator.

- [ ] **Step 8: Commit deploy artifacts**

```bash
git add typescript/deploy/voynich-grounder/
git commit -m "feat(voynich): deploy grounder agent to Databricks Apps"
```
