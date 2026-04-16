-- =============================================================================
-- DELTA LAKE SCHEMA — voynich catalog
-- Run these in a Databricks notebook or SQL editor before first deployment
-- =============================================================================

CREATE CATALOG IF NOT EXISTS voynich;

-- -----------------------------------------------------------------------
-- Corpus tables (populated from EVA transliteration + illustration data)
-- -----------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS voynich.corpus;

CREATE TABLE IF NOT EXISTS voynich.corpus.eva_chars (
    page        INT,
    section     STRING,      -- herbal | astronomical | balneological | pharmaceutical | recipes
    paragraph   INT,
    word_pos    INT,
    char_pos    INT,
    symbol      STRING,      -- EVA character(s)
    context     STRING       -- surrounding 5 chars for n-gram analysis
) USING DELTA
PARTITIONED BY (section)
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS voynich.corpus.eva_words (
    page        INT,
    section     STRING,
    paragraph   INT,
    word_pos    INT,
    word        STRING,      -- full EVA word token
    word_length INT
) USING DELTA
PARTITIONED BY (section);

CREATE TABLE IF NOT EXISTS voynich.corpus.illustration_metadata (
    page                   INT,
    section                STRING,
    illustration_type      STRING,   -- plant | star_chart | figure | recipe | diagram | cosmological
    identified_subjects    STRING,   -- scholarly identification of illustrated objects
    color_palette          STRING,   -- JSON array of colors present
    semantic_tags          STRING,   -- JSON array of semantic concepts
    scholarly_interpretation STRING  -- Consensus scholarly interpretation if any
) USING DELTA;

CREATE TABLE IF NOT EXISTS voynich.corpus.decoded_word_registry (
    -- Tracks how each EVA word is decoded by top hypothesis per generation
    eva_word       STRING,
    decoded_word   STRING,
    hypothesis_id  STRING,
    generation     INT,
    section        STRING,
    confidence     DOUBLE
) USING DELTA
PARTITIONED BY (generation);

-- -----------------------------------------------------------------------
-- Evolution tables (populated by the loop)
-- -----------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS voynich.evolution;

CREATE TABLE IF NOT EXISTS voynich.evolution.population (
    id                  STRING,
    generation          INT,
    parent_id           STRING,
    cipher_type         STRING,
    source_language     STRING,
    symbol_map          STRING,      -- JSON
    null_chars          STRING,      -- JSON array
    transformation_rules STRING,     -- JSON array
    fitness_statistical  DOUBLE,
    fitness_perplexity   DOUBLE,
    fitness_semantic     DOUBLE,
    fitness_consistency  DOUBLE,
    fitness_adversarial  DOUBLE,
    fitness_composite    DOUBLE,
    agent_eval_historian DOUBLE,
    agent_eval_critic    DOUBLE,
    decoded_sample       STRING,
    mlflow_run_id        STRING,
    flagged_for_review   BOOLEAN DEFAULT FALSE,
    created_at           TIMESTAMP DEFAULT current_timestamp()
) USING DELTA
PARTITIONED BY (generation)
TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.autoOptimize.optimizeWrite' = 'true'
);

CREATE TABLE IF NOT EXISTS voynich.evolution.review_queue (
    hypothesis_id  STRING,
    reason         STRING,
    expert_type    STRING,   -- cryptographer | medievalist | botanist | astronomer
    status         STRING DEFAULT 'pending',   -- pending | in_review | resolved | dismissed
    annotation     STRING,   -- expert annotation when resolved
    flagged_at     TIMESTAMP DEFAULT current_timestamp(),
    resolved_at    TIMESTAMP
) USING DELTA;

CREATE TABLE IF NOT EXISTS voynich.evolution.constraints (
    -- Researcher-injected constraints picked up by Decipherer each generation
    id               BIGINT GENERATED ALWAYS AS IDENTITY,
    constraint_type  STRING,   -- force_language | ban_cipher_type | fix_symbol | require_section_vocab
    constraint_value STRING,
    target_section   STRING,
    active           BOOLEAN DEFAULT TRUE,
    created_by       STRING,
    created_at       TIMESTAMP DEFAULT current_timestamp()
) USING DELTA;

CREATE TABLE IF NOT EXISTS voynich.evolution.agent_evals (
    -- Judge agent eval scores per agent per generation
    agent_name              STRING,
    hypothesis_id           STRING,
    generation              INT,
    tool_use_score          DOUBLE,
    reasoning_quality       DOUBLE,
    hallucination_confidence DOUBLE,
    composite_eval_score    DOUBLE,
    action_triggered        STRING,   -- OK | AGENT_DOWNWEIGHTED | PROMPT_REFINEMENT_FLAGGED
    mlflow_run_id           STRING,
    created_at              TIMESTAMP DEFAULT current_timestamp()
) USING DELTA
PARTITIONED BY (generation);

-- -----------------------------------------------------------------------
-- Medieval corpus tables (populated from digitized medieval texts)
-- -----------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS voynich.medieval;

-- These back the Vector Search indexes used by the Historian agent
CREATE TABLE IF NOT EXISTS voynich.medieval.botanical_source (
    id           BIGINT GENERATED ALWAYS AS IDENTITY,
    text         STRING,      -- passage text
    source       STRING,      -- e.g. "Dioscorides De Materia Medica"
    author       STRING,
    date_ce      STRING,      -- e.g. "ca. 77 CE" or "ca. 1150 CE"
    language     STRING,      -- latin | greek | arabic
    section_type STRING,      -- botanical | medicinal | preparation
    embedding    ARRAY<FLOAT> -- populated by Vector Search indexer
) USING DELTA;

-- Similar tables: voynich.medieval.astronomical_source,
--                 voynich.medieval.pharmaceutical_source,
--                 voynich.medieval.alchemical_source

---

-- =============================================================================
-- DATABRICKS WORKFLOW YAML — voynich_evolution_job.yml
-- Deploy via: databricks jobs create --json @voynich_evolution_job.yml
-- =============================================================================

# name: voynich-evolutionary-search
# description: "Runs evolutionary cryptanalysis loop for Voynich manuscript decipherment"
# 
# schedule:
#   quartz_cron_expression: "0 0 2 * * ?"   # Nightly at 2am, or trigger manually
#   timezone_id: "America/Los_Angeles"
#   pause_status: PAUSED   # Start paused; trigger manually for first run
#
# job_clusters:
#   - job_cluster_key: orchestrator_cluster
#     new_cluster:
#       spark_version: "15.4.x-scala2.12"
#       node_type_id: "m5.xlarge"
#       num_workers: 1
#       spark_conf:
#         spark.databricks.delta.preview.enabled: "true"
#
# tasks:
#   - task_key: initialize_corpus
#     description: "Load EVA transliteration and illustration metadata into Delta"
#     job_cluster_key: orchestrator_cluster
#     notebook_task:
#       notebook_path: /voynich/notebooks/01_load_corpus
#     timeout_seconds: 1800
#
#   - task_key: build_vector_indexes
#     depends_on: [{task_key: initialize_corpus}]
#     description: "Build/refresh Vector Search indexes for medieval corpora"
#     job_cluster_key: orchestrator_cluster
#     notebook_task:
#       notebook_path: /voynich/notebooks/02_build_indexes
#     timeout_seconds: 3600
#
#   - task_key: seed_population
#     depends_on: [{task_key: build_vector_indexes}]
#     description: "Generate initial population (generation 0) via Decipherer agent"
#     job_cluster_key: orchestrator_cluster
#     python_wheel_task:
#       package_name: "voynich-orchestrator"
#       entry_point: "seed_population"
#       named_parameters:
#         n: "500"
#     libraries:
#       - whl: /dbfs/voynich/wheels/voynich_orchestrator-0.1.0-py3-none-any.whl
#
#   - task_key: run_evolution_batch_1
#     depends_on: [{task_key: seed_population}]
#     description: "Generations 1-500 (first batch)"
#     job_cluster_key: orchestrator_cluster
#     python_wheel_task:
#       package_name: "voynich-orchestrator"
#       entry_point: "run_generation_batch"
#       named_parameters:
#         n_generations: "500"
#     libraries:
#       - whl: /dbfs/voynich/wheels/voynich_orchestrator-0.1.0-py3-none-any.whl
#     timeout_seconds: 21600   # 6 hours
#
#   - task_key: human_review_gate
#     depends_on: [{task_key: run_evolution_batch_1}]
#     description: "Pause for researcher review of top candidates. Manual approval required."
#     job_cluster_key: orchestrator_cluster
#     notebook_task:
#       notebook_path: /voynich/notebooks/03_review_gate
#       base_parameters:
#         review_url: "https://voynich-orchestrator.databricksapps.com/_apx/agent"
#
#   - task_key: run_evolution_batch_2
#     depends_on: [{task_key: human_review_gate}]
#     description: "Generations 501-2000 (final batch, with researcher constraints injected)"
#     job_cluster_key: orchestrator_cluster
#     python_wheel_task:
#       package_name: "voynich-orchestrator"
#       entry_point: "run_generation_batch"
#       named_parameters:
#         n_generations: "1500"
#     libraries:
#       - whl: /dbfs/voynich/wheels/voynich_orchestrator-0.1.0-py3-none-any.whl
#     timeout_seconds: 64800   # 18 hours
#
# email_notifications:
#   on_failure: ["stuagano@databricks.com"]
#   on_success: ["stuagano@databricks.com"]
#
# parameters:
#   - name: n_generations
#     default: "500"
