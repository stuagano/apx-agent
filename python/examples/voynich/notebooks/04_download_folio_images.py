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
import re

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
