# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Load EVA Corpus into Delta Lake
# MAGIC
# MAGIC Ingests the EVA transliteration of the Voynich manuscript and illustration
# MAGIC metadata into the `voynich.corpus` schema.
# MAGIC
# MAGIC **Sources:**
# MAGIC - EVA transliteration: `voynich.nu` interlinear file (ZL transliteration)
# MAGIC - Illustration metadata: compiled from Beinecke digital scans + scholarly notes
# MAGIC
# MAGIC **Run once** before the first evolutionary loop job. Re-run to refresh corpus.

# COMMAND ----------

# MAGIC %pip install apx-agent>=0.16.0 requests

# COMMAND ----------

import re
import json
import requests
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import (
    StructType, StructField,
    StringType, IntegerType
)

spark = SparkSession.builder.getOrCreate()

# COMMAND ----------

# MAGIC %md ## 1. Create schema

# COMMAND ----------

spark.sql("CREATE CATALOG IF NOT EXISTS voynich")
spark.sql("CREATE SCHEMA IF NOT EXISTS voynich.corpus")
spark.sql("CREATE SCHEMA IF NOT EXISTS voynich.evolution")
spark.sql("CREATE SCHEMA IF NOT EXISTS voynich.medieval")

print("✓ Schemas created")

# COMMAND ----------

# MAGIC %md ## 2. Load EVA interlinear file
# MAGIC
# MAGIC The ZL transliteration is the most complete available.
# MAGIC Download from voynich.nu or load from DBFS if already staged.

# COMMAND ----------

EVA_DBFS_PATH = "/dbfs/voynich/eva_interlinear_zl.txt"

# If file isn't staged, download it
import os
if not os.path.exists(EVA_DBFS_PATH):
    # Alternatively: spark.sparkContext.addFile("https://...") 
    # For now, raise an informative error
    raise FileNotFoundError(
        f"EVA file not found at {EVA_DBFS_PATH}.\n"
        "Please stage the ZL interlinear file from voynich.nu to DBFS first:\n"
        "  dbutils.fs.cp('path/to/eva_interlinear_zl.txt', 'dbfs:/voynich/eva_interlinear_zl.txt')"
    )

# COMMAND ----------

# MAGIC %md ### Parse the interlinear format
# MAGIC
# MAGIC EVA interlinear format:
# MAGIC ```
# MAGIC <f1r.P1.1;H>         fachys.ykal.ar.ataiin.shol.shory.cth...
# MAGIC <f1r.P1.2;H>         ykal.ar.ataiin...
# MAGIC ```
# MAGIC Format: `<folio.paragraph.line;hand>  word1.word2.word3`

# COMMAND ----------

SECTION_MAP = {
    "f1":  "herbal",   "f2":  "herbal",   "f3":  "herbal",   "f4":  "herbal",
    "f5":  "herbal",   "f6":  "herbal",   "f7":  "herbal",   "f8":  "herbal",
    "f9":  "herbal",   "f10": "herbal",   "f11": "herbal",   "f12": "herbal",
    "f13": "herbal",   "f14": "herbal",   "f15": "herbal",   "f16": "herbal",
    "f17": "herbal",   "f18": "herbal",   "f19": "herbal",   "f20": "herbal",
    "f25": "astronomical", "f26": "astronomical", "f27": "astronomical",
    "f28": "astronomical", "f29": "astronomical", "f30": "astronomical",
    "f31": "astronomical", "f32": "astronomical", "f33": "astronomical",
    "f34": "astronomical", "f40": "astronomical",
    "f75": "balneological","f76": "balneological","f77": "balneological",
    "f78": "balneological","f79": "balneological","f80": "balneological",
    "f82": "balneological","f83": "balneological","f84": "balneological",
    "f85": "balneological",
    "f87": "cosmological","f86": "cosmological",
    "f99": "pharmaceutical","f100": "pharmaceutical","f101": "pharmaceutical",
    "f102": "pharmaceutical",
    "f103": "recipes","f104": "recipes","f105": "recipes",
    "f106": "recipes","f107": "recipes","f108": "recipes",
    "f109": "recipes","f110": "recipes","f111": "recipes",
    "f112": "recipes","f113": "recipes","f114": "recipes",
    "f115": "recipes","f116": "recipes",
}

def guess_section(folio: str) -> str:
    base = re.sub(r'[rv]$', '', folio.lower())
    return SECTION_MAP.get(base, "unknown")

def parse_eva_line(line: str) -> list[dict]:
    """Parse one EVA interlinear line into word + char records."""
    line = line.strip()
    if not line or line.startswith('#'):
        return []

    match = re.match(r'<(f\d+[rv]?)\.(\w+)\.(\d+);(\w+)>\s+(.*)', line)
    if not match:
        return []

    folio, para_code, line_num, hand, text = match.groups()
    page_num = int(re.sub(r'[rv]', '', folio))
    section  = guess_section(folio)

    records = []
    words = [w for w in text.split('.') if w and w not in ('-', '!', '{', '}')]
    for word_pos, word in enumerate(words):
        word = re.sub(r'[{}\-!*]', '', word).strip()
        if not word:
            continue
        # EVA multi-char glyphs: ch, sh, th, qo, etc. — treat as atomic tokens
        for char_pos, char in enumerate(re.findall(r'ch|sh|th|qo|[a-z]', word)):
            records.append({
                "page":     page_num,
                "section":  section,
                "paragraph": int(line_num),
                "word_pos": word_pos,
                "char_pos": char_pos,
                "symbol":   char,
                "context":  word,
            })
    return records

# COMMAND ----------

# Parse all lines
all_chars = []
all_words = []

with open(EVA_DBFS_PATH, 'r', encoding='utf-8') as f:
    for raw_line in f:
        records = parse_eva_line(raw_line)
        all_chars.extend(records)

        # Also build word-level records
        m = re.match(r'<(f\d+[rv]?)\.(\w+)\.(\d+);(\w+)>\s+(.*)', raw_line.strip())
        if m:
            folio, _, line_num, _, text = m.groups()
            page_num = int(re.sub(r'[rv]', '', folio))
            section  = guess_section(folio)
            words = [re.sub(r'[{}\-!*]', '', w).strip()
                     for w in text.split('.') if w and w not in ('-', '!', '{', '}')]
            for word_pos, word in enumerate(words):
                if word:
                    all_words.append({
                        "page":       page_num,
                        "section":    section,
                        "paragraph":  int(line_num),
                        "word_pos":   word_pos,
                        "word":       word,
                        "word_length": len(re.findall(r'ch|sh|th|qo|[a-z]', word)),
                    })

print(f"✓ Parsed {len(all_chars):,} characters across {len(all_words):,} words")

# COMMAND ----------

# Write to Delta
chars_df = spark.createDataFrame(all_chars)
words_df = spark.createDataFrame(all_words)

(chars_df.write
    .format("delta")
    .mode("overwrite")
    .partitionBy("section")
    .option("overwriteSchema", "true")
    .saveAsTable("voynich.corpus.eva_chars"))

(words_df.write
    .format("delta")
    .mode("overwrite")
    .partitionBy("section")
    .option("overwriteSchema", "true")
    .saveAsTable("voynich.corpus.eva_words"))

print("✓ EVA corpus written to Delta")
display(spark.sql("SELECT section, COUNT(*) as chars FROM voynich.corpus.eva_chars GROUP BY section ORDER BY chars DESC"))

# COMMAND ----------

# MAGIC %md ## 3. Load illustration metadata

# COMMAND ----------

# Illustration metadata — compiled from Beinecke catalog and scholarly sources.
# This is the seed dataset; researchers can augment via the Apps review interface.

ILLUSTRATION_DATA = [
    # (page, section, type, subjects, colors, semantic_tags, interpretation)
    (1,  "herbal",  "plant",     "Unidentified plant, possibly sunflower family",
     '["green","brown","red"]', '["plant","root","flower","stem","leaf"]',
     "Plant with broad leaves; roots visible; red berries or flowers"),
    (2,  "herbal",  "plant",     "Unidentified plant, aquatic features",
     '["green","blue"]', '["plant","water","leaf","stem"]',
     "Aquatic or marsh plant; blue coloring suggests water association"),
    (25, "astronomical", "star_chart", "Zodiac Aries with star positions",
     '["gold","red","brown"]', '["star","zodiac","aries","celestial","sign"]',
     "Zodiac wheel section, Aries. Stars marked with labels."),
    (29, "astronomical", "star_chart", "Star chart with nymphs, Taurus",
     '["gold","green","red"]', '["star","zodiac","taurus","nymph","celestial"]',
     "Taurus zodiac section. Female figures (nymphs) holding stars."),
    (75, "balneological", "figure", "Female figures in pools/baths",
     '["blue","green","red"]', '["water","bath","figure","body","vessel","pool"]',
     "Bathing scene. Multiple female figures in interconnected pools."),
    (82, "balneological", "figure", "Female figures, anatomical diagram?",
     '["blue","red"]', '["body","figure","water","anatomy"]',
     "Figures appear to show body parts or internal structures."),
    (99, "pharmaceutical", "recipe", "Pharmaceutical jars and plant materials",
     '["green","brown","red"]', '["vessel","ingredient","preparation","recipe","jar"]',
     "Pharmaceutical preparation scene. Labeled containers."),
    (103, "recipes", "recipe", "Recipe text with plant material",
     '["green","brown"]', '["recipe","ingredient","take","mix","preparation"]',
     "Dense recipe text. Possibly ingredient lists."),
]

schema = StructType([
    StructField("page",                   IntegerType(), False),
    StructField("section",                StringType(),  False),
    StructField("illustration_type",      StringType(),  True),
    StructField("identified_subjects",    StringType(),  True),
    StructField("color_palette",          StringType(),  True),
    StructField("semantic_tags",          StringType(),  True),
    StructField("scholarly_interpretation", StringType(), True),
])

rows = [
    (p, s, t, subj, col, tags, interp)
    for p, s, t, subj, col, tags, interp in ILLUSTRATION_DATA
]

illus_df = spark.createDataFrame(rows, schema=schema)
(illus_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable("voynich.corpus.illustration_metadata"))

print(f"✓ Loaded {len(rows)} illustration records")
print("⚠️  This is a seed dataset. Expand via the researcher annotation UI.")

# COMMAND ----------

# MAGIC %md ## 4. Bootstrap evolution schema

# COMMAND ----------

for ddl in [
    """CREATE TABLE IF NOT EXISTS voynich.evolution.population (
        id STRING NOT NULL, generation INT NOT NULL, parent_id STRING,
        cipher_type STRING, source_language STRING,
        symbol_map STRING, null_chars STRING, transformation_rules STRING,
        fitness_statistical DOUBLE DEFAULT 0.0, fitness_perplexity DOUBLE DEFAULT 0.0,
        fitness_semantic DOUBLE DEFAULT 0.0, fitness_consistency DOUBLE DEFAULT 0.0,
        fitness_adversarial DOUBLE DEFAULT 0.0, fitness_composite DOUBLE DEFAULT 0.0,
        agent_eval_historian DOUBLE DEFAULT 0.0, agent_eval_critic DOUBLE DEFAULT 0.0,
        decoded_sample STRING, mlflow_run_id STRING,
        flagged_for_review BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT current_timestamp()
    ) USING DELTA PARTITIONED BY (generation)
    TBLPROPERTIES ('delta.enableChangeDataFeed'='true','delta.autoOptimize.optimizeWrite'='true')""",

    """CREATE TABLE IF NOT EXISTS voynich.evolution.review_queue (
        hypothesis_id STRING, reason STRING, expert_type STRING,
        status STRING DEFAULT 'pending', annotation STRING,
        flagged_at TIMESTAMP DEFAULT current_timestamp(), resolved_at TIMESTAMP
    ) USING DELTA""",

    """CREATE TABLE IF NOT EXISTS voynich.evolution.constraints (
        id BIGINT GENERATED ALWAYS AS IDENTITY,
        constraint_type STRING, constraint_value STRING, target_section STRING,
        active BOOLEAN DEFAULT TRUE, created_by STRING,
        created_at TIMESTAMP DEFAULT current_timestamp()
    ) USING DELTA""",

    """CREATE TABLE IF NOT EXISTS voynich.evolution.agent_evals (
        agent_name STRING, hypothesis_id STRING, generation INT,
        tool_use_score DOUBLE, reasoning_quality DOUBLE,
        hallucination_confidence DOUBLE, composite_eval_score DOUBLE,
        action_triggered STRING, mlflow_run_id STRING,
        created_at TIMESTAMP DEFAULT current_timestamp()
    ) USING DELTA PARTITIONED BY (generation)""",

    """CREATE TABLE IF NOT EXISTS voynich.corpus.decoded_word_registry (
        eva_word STRING, decoded_word STRING, hypothesis_id STRING,
        generation INT, section STRING, confidence DOUBLE
    ) USING DELTA PARTITIONED BY (generation)""",
]:
    spark.sql(ddl)
    print(f"✓ {ddl.split('(')[0].strip()}")

print("\n✓ Evolution schema ready")
