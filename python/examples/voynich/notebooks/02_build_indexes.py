# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Build Vector Search Indexes for Medieval Corpora
# MAGIC
# MAGIC Creates and populates the Vector Search indexes used by the Historian agent.
# MAGIC
# MAGIC **Indexes created:**
# MAGIC - `{_CATALOG}.voynich_medieval.botanical_index`     — Dioscorides, Hildegard, Apuleius Platonicus
# MAGIC - `{_CATALOG}.voynich_medieval.astronomical_index`  — Ptolemy, Sacrobosco, Arabic star catalogs
# MAGIC - `{_CATALOG}.voynich_medieval.pharmaceutical_index`— Antidotarium Nicolai, Circa instans
# MAGIC - `{_CATALOG}.voynich_medieval.alchemical_index`    — Pseudo-Lull, Jabir corpus
# MAGIC
# MAGIC **Prerequisites:** Notebook 01 must have run. Vector Search endpoint must exist.

# COMMAND ----------

# MAGIC %pip install apx-agent>=0.16.0 databricks-vectorsearch

# COMMAND ----------

import os
import json
from databricks.vector_search.client import VectorSearchClient
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.getOrCreate()
vsc   = VectorSearchClient()
_CATALOG = os.getenv("VOYNICH_CATALOG", "serverless_stable_s0v155_catalog")

VECTOR_SEARCH_ENDPOINT = "voynich_vs_endpoint"   # set to your VS endpoint name

# COMMAND ----------

# MAGIC %md ## 1. Create Vector Search endpoint (if needed)

# COMMAND ----------

try:
    vsc.get_endpoint(VECTOR_SEARCH_ENDPOINT)
    print(f"✓ Endpoint '{VECTOR_SEARCH_ENDPOINT}' already exists")
except Exception:
    vsc.create_endpoint(
        name=VECTOR_SEARCH_ENDPOINT,
        endpoint_type="STANDARD",
    )
    print(f"✓ Created endpoint '{VECTOR_SEARCH_ENDPOINT}'")

# COMMAND ----------

# MAGIC %md ## 2. Load seed medieval texts into Delta source tables
# MAGIC
# MAGIC These are short representative passages — expand with full digitized corpora.
# MAGIC Public domain sources: Project Gutenberg Latin texts, Internet Archive, Perseus Digital Library.

# COMMAND ----------

BOTANICAL_PASSAGES = [
    # (text, source, author, date_ce, language, section_type)
    ("The root of this plant when boiled in water and drunk relieves pain of the stomach and purges black bile.",
     "De Materia Medica Book I", "Dioscorides", "ca. 77 CE", "greek_latin", "medicinal"),
    ("The leaves applied as a poultice reduce swelling and draw out thorns and splinters from the flesh.",
     "De Materia Medica Book II", "Dioscorides", "ca. 77 CE", "greek_latin", "preparation"),
    ("This herb grows in moist places near streams. Its flowers are white and it blooms in early summer.",
     "Physica Liber I", "Hildegard of Bingen", "ca. 1150 CE", "latin", "botanical"),
    ("The juice of this plant mixed with honey and warm water is good for those who cough and have difficulty breathing.",
     "Physica Liber II", "Hildegard of Bingen", "ca. 1150 CE", "latin", "medicinal"),
    ("Taken with wine it is beneficial to the liver and kidneys and dissolves stones.",
     "Herbarius Apulei", "Apuleius Platonicus", "ca. 400 CE", "latin", "medicinal"),
    ("The root dried and powdered and mixed with oil of roses cures headaches when applied to the temples.",
     "Circa instans", "Matthaeus Platearius", "ca. 1150 CE", "latin", "preparation"),
    ("This plant is hot in the third degree and dry in the second. It opens obstructions of the liver.",
     "Antidotarium Nicolai", "Nicholas of Salerno", "ca. 1100 CE", "latin", "pharmaceutical"),
    ("The seed ground fine and mixed with vinegar removes spots and blemishes from the skin.",
     "De viribus herbarum", "Macer Floridus", "ca. 1000 CE", "latin", "preparation"),
    ("Plant the seeds in spring after the last frost. Harvest the roots in autumn when the leaves yellow.",
     "Ruralia commoda", "Pietro de Crescenzi", "ca. 1304 CE", "latin", "botanical"),
    ("Against the bite of serpents: take this root and grind it and place it upon the wound with salt.",
     "De Materia Medica Book VI", "Dioscorides", "ca. 77 CE", "greek_latin", "medicinal"),
]

ASTRONOMICAL_PASSAGES = [
    ("The stars of Aries are seventeen in number. The star on the head is of the second magnitude.",
     "Almagest Book VII", "Ptolemy", "ca. 150 CE", "greek_latin", "stellar"),
    ("When the sun enters Aries the days and nights become equal. This is the vernal equinox.",
     "De sphaera mundi", "Sacrobosco", "ca. 1230 CE", "latin", "calendrical"),
    ("The moon completes her circuit through the twelve signs in approximately twenty-eight days.",
     "De sphaera mundi", "Sacrobosco", "ca. 1230 CE", "latin", "calendrical"),
    ("Saturn is the highest of the seven planets. Its circle is completed in thirty years.",
     "Almagest Book IX", "Ptolemy", "ca. 150 CE", "greek_latin", "planetary"),
    ("Al-Thurayya the Pleiades: six visible stars in the shoulder of Taurus, associated with rain.",
     "Book of Fixed Stars", "Al-Sufi", "ca. 964 CE", "arabic_latin", "stellar"),
    ("Jupiter completes its revolution through all signs in twelve years, moving about one sign per year.",
     "Theorica planetarum", "Gerard of Cremona", "ca. 1150 CE", "latin", "planetary"),
    ("The zodiac is an oblique circle crossing the equator at two points: the head of Aries and the head of Libra.",
     "De sphaera mundi", "Sacrobosco", "ca. 1230 CE", "latin", "cosmological"),
    ("A lunar eclipse occurs when the earth's shadow falls upon the moon at opposition.",
     "Almagest Book VI", "Ptolemy", "ca. 150 CE", "greek_latin", "astronomical"),
]

PHARMACEUTICAL_PASSAGES = [
    ("Take of theriac the weight of a hazelnut, dissolve in warm wine, and give to the patient fasting.",
     "Antidotarium Nicolai", "Nicholas of Salerno", "ca. 1100 CE", "latin", "compound"),
    ("Compound of roses: take two drachms of dried rose petals, one drachm of cinnamon, half a drachm of cloves.",
     "Circa instans", "Matthaeus Platearius", "ca. 1150 CE", "latin", "recipe"),
    ("Syrup of violets: crush fresh violets, strain through cloth, add twice their weight in sugar, boil gently.",
     "Antidotarium Nicolai", "Nicholas of Salerno", "ca. 1100 CE", "latin", "preparation"),
    ("This electuary is good against melancholy. It warms the brain and opens the senses.",
     "Regimen sanitatis Salernitanum", "School of Salerno", "ca. 1100 CE", "latin", "medicinal"),
    ("For a fever of the third type: bleed from the right arm, then give a decoction of willow bark.",
     "Rosa anglica", "John of Gaddesden", "ca. 1314 CE", "latin", "treatment"),
]

ALCHEMICAL_PASSAGES = [
    ("The philosopherf's work begins with the purification of mercury. Remove all impurity by sublimation.",
     "Summa perfectionis", "Pseudo-Jabir", "ca. 1300 CE", "latin", "process"),
    ("Dissolve the body in the water until the water becomes red as blood. This is the first work.",
     "Testamentum", "Pseudo-Lull", "ca. 1330 CE", "latin", "process"),
    ("Sulphur and mercury are the two principles of all metals. Gold is perfect sulphur and perfect mercury.",
     "De mineralibus", "Albertus Magnus", "ca. 1250 CE", "latin", "theory"),
    ("The green lion devours the sun. This signifies the dissolution of gold in vitriol.",
     "Rosarium philosophorum", "Anonymous", "ca. 1350 CE", "latin", "symbolic"),
]

# Write to Delta source tables
for corpus_name, passages in [
    ("botanical",      BOTANICAL_PASSAGES),
    ("astronomical",   ASTRONOMICAL_PASSAGES),
    ("pharmaceutical", PHARMACEUTICAL_PASSAGES),
    ("alchemical",     ALCHEMICAL_PASSAGES),
]:
    rows = [
        {"text": text, "source": src, "author": auth, "date_ce": date,
         "language": lang, "section_type": stype}
        for text, src, auth, date, lang, stype in passages
    ]
    df = spark.createDataFrame(rows)
    (df.write
        .format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(f"{_CATALOG}.voynich_medieval.{corpus_name}_source"))
    print(f"✓ {corpus_name}: {len(rows)} passages")

# COMMAND ----------

# MAGIC %md ## 3. Create Delta Sync Vector Search indexes

# COMMAND ----------

INDEXES = {
    "botanical":      (f"{_CATALOG}.voynich_medieval.botanical_source",      f"{_CATALOG}.voynich_medieval.botanical_index"),
    "astronomical":   (f"{_CATALOG}.voynich_medieval.astronomical_source",   f"{_CATALOG}.voynich_medieval.astronomical_index"),
    "pharmaceutical": (f"{_CATALOG}.voynich_medieval.pharmaceutical_source", f"{_CATALOG}.voynich_medieval.pharmaceutical_index"),
    "alchemical":     (f"{_CATALOG}.voynich_medieval.alchemical_source",     f"{_CATALOG}.voynich_medieval.alchemical_index"),
}

for corpus, (source_table, index_name) in INDEXES.items():
    # Enable Change Data Feed on source table (required for Delta Sync index)
    spark.sql(f"""
        ALTER TABLE {source_table}
        SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')
    """)

    try:
        # Check if index exists
        vsc.get_index(VECTOR_SEARCH_ENDPOINT, index_name)
        print(f"✓ Index '{index_name}' already exists — syncing...")
        vsc.get_index(VECTOR_SEARCH_ENDPOINT, index_name).sync()
    except Exception:
        print(f"Creating index '{index_name}'...")
        vsc.create_delta_sync_index(
            endpoint_name=VECTOR_SEARCH_ENDPOINT,
            index_name=index_name,
            source_table_name=source_table,
            pipeline_type="TRIGGERED",
            primary_key="id",                    # add id col in next step
            embedding_source_column="text",
            embedding_model_endpoint_name="databricks-gte-large-en",
        )
        print(f"✓ Index '{index_name}' created")

# COMMAND ----------

# MAGIC %md ## 4. Verify indexes are queryable

# COMMAND ----------

import time
time.sleep(30)  # Allow sync to complete

test_query = "plant with red flowers used for stomach ailments"
for corpus, (_, index_name) in INDEXES.items():
    try:
        index = vsc.get_index(VECTOR_SEARCH_ENDPOINT, index_name)
        results = index.similarity_search(
            query_text=test_query,
            columns=["text", "source", "author"],
            num_results=2,
        )
        hits = results.get("result", {}).get("data_array", [])
        print(f"✓ {corpus}: {len(hits)} results for test query")
    except Exception as e:
        print(f"⚠️  {corpus}: {e}")

print("\n✓ Notebook 02 complete — indexes ready for Historian agent")
