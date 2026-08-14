<!--
  Agent example README template.

  Copy this file to your example's directory as README.md and fill in each
  section. It codifies the majority convention already used across
  python/examples (data-inspector, contract-parsing-agent, eligibility-agent,
  shortage-intelligence-agent, account-search-service, afr-enrollment-api, ...)
  so new examples don't drift into a new shape.

  Rules of thumb:
  - Keep the "## What it does" opener — that exact heading, not "How it works"
    / "What you'll learn" / "What makes this simple". One tight paragraph.
  - Keep the three numbered Parts in this order and with these exact titles.
    They're what readers scan for; identical titles make every example
    navigable the same way.
  - The sections marked (optional) are include-if-relevant. Delete the ones
    that don't apply rather than leaving them empty.
  - Delete this comment block and the (guidance) notes before committing.
-->

# <Agent Name>

<!-- One-paragraph elevator pitch: what problem it solves and the headline
     apx-agent capability it demonstrates. Bold the key primitive, e.g.
     **DataAgent + RouterAgent**, **HandoffAgent**, **A2A sub_agents**. -->

## What it does

<!-- 2-5 sentences. What the agent does, what data/tools it touches, and the
     one thing a reader should take away. If it's part of a multi-app
     architecture, say so and link the siblings here. -->

## Prerequisites

<!-- Bullet list: Databricks workspace + CLI profile, a SQL warehouse, any
     Unity Catalog objects, Vector Search endpoint, Genie space, secrets, etc.
     Only what's actually required to run Parts 1-3. -->

- Databricks workspace with a configured CLI profile
- A running SQL warehouse (serverless auto-discovered where supported)
- <other requirements>

## Part 1: Workspace setup (one-time)

<!-- Everything that must exist in the workspace before local dev works:
     catalog/schema creation, table seeding, VS index, Genie space, secrets.
     Use numbered ### Step N: subsections. If there is genuinely no workspace
     setup, say "No one-time workspace setup is required." and keep the
     heading so the three-Part shape is preserved. -->

### Step 1: <first setup step>

## Part 2: Local development

<!-- Install, configure the CLI profile, run tests, run the agent locally.
     Use numbered ### Step N: subsections. Show the exact commands. -->

### Step 1: Install

```bash
uv sync
```

### Step 2: Run the tests

```bash
uv run pytest
```

### Step 3: Run locally

```bash
uv run uvicorn app:app --reload
```

## Part 3: Deploy to Databricks Apps

<!-- Review app.yml (note: app.yml, not app.yaml), then deploy + verify.
     Use numbered ### Step N: subsections. -->

### Step 1: Review `app.yml`

### Step 2: Deploy

```bash
apx-agent deploy --target apps
```

### Step 3: Verify

## Configuration

<!-- (optional) Table of env vars / settings the agent reads. Prefer a table:
     | Variable | Default | What it controls | -->

## Tools

<!-- (optional) The tools this agent exposes, one line each. For an API/service
     example, title this "## API reference" instead. -->

## Knowledge (OKF) bundle

<!-- (optional — include for DATA-GROUNDED examples: DataAgent / anything over
     governed UC tables or functions.) Ship an `.apx/okf/` bundle so the agent is
     grounded and the dev-UI **Knowledge** tab (`/_apx/knowledge`) shows content.
     A bundle is:
       .apx/okf/
       ├── datasets/<schema>.md     # frontmatter + # Tables + # Glossary (### Term / def / Synonyms:)
       ├── tables/<table>.md        # frontmatter + # Overview + # Schema table + # Examples
       ├── tables/index.md
       └── index.md
     Wire it into the agent with `knowledge="./.apx/okf"`, and make sure your
     databricks.yml build rule copies it: `cp -r .apx .build/`. See
     python/examples/precall-brief (functions + views), bakehouse-agent, and
     hubspot-complaints-agent (tables) for worked bundles. Non-data examples
     (tool/MCP/handoff only) can omit this — delete the section. -->

## Project structure

<!-- (optional but recommended) A short tree of the important files. Frontend
     source, if any, lives in client/ (not ui/). -->

```
<example-name>/
├── agent.py                 # Agent definition
├── app.py                   # Local run entrypoint
├── app.yml                  # Databricks Apps entrypoint + env
├── databricks.yml           # Bundle config
├── agent_server/            # Deploy bootstrap (don't edit)
├── .apx/okf/                # (data-grounded examples) OKF bundle → Knowledge tab
└── tests/
```

## Troubleshooting

<!-- (optional) Common failure -> fix, as a list or table. -->
