# **Data Triage Agent — Product Requirements Document**

**Status:** Draft for Discussion

**Date:** March 30, 2026

**Author:** Stuart Gano, Databricks

**Audience:** Justin (Uplight Engineering)

———

## **Problem Statement**

When data doesn't show up in an API or downstream system as expected, support and data engineering teams currently have to manually chase the problem across multiple systems:
1. Query Databricks tables to check if the data exists
2. Trace Unity Catalog lineage to find upstream sources
3. Pull up Databricks job logs to check for pipeline failures
4. Read pipeline source code to look for filters or transformations that may have dropped the data
5. Read API source code to check for application-level filtering

This process is slow, requires deep system knowledge, and typically falls on a senior engineer. The goal is to automate the investigation so that **customer support teams can self-serve** — and eventually, so the agent takes a first pass automatically when a support ticket is created, surfacing an initial diagnosis before a human even looks at it.

———

## **Proposed Solution**

A conversational AI agent deployed as a Databricks App that can investigate data lineage issues end-to-end. A support engineer describes the problem in plain language; the agent queries the relevant systems, follows the data from source to API, and returns a plain-language explanation of where and why the data was dropped.

**Example interaction:**

*"Account 12345 isn't showing up in the demand-response API. Can you investigate?"*

**Agent output:**

*"Account 12345 exists in the `silver.accounts` table but was filtered out during the gold-layer transform. The pipeline at `pipelines/accounts_gold_transform.py` (line 142) excludes accounts where `dr_enrolled = false`. Account 12345 has `dr_enrolled = false` as of 2026-03-28. The data never reached `gold.dr_accounts`, so the API is working correctly — this is a data issue, not an API bug."*

———

## **Architecture**

The agent is built on **Databricks Apps + APX** (Uplight's existing app framework) using the same pattern as the Explain My Bill prototype, but with tools that span the full data + code stack.

┌──────────────────────────────────────────────────────────────────────────┐

│  USERS                                                                   │

│                                                                          │

│   Support Engineer                   Claude Desktop / Cursor / Agent    │

│   (browser chat UI)                  (MCP client — optional)            │

└───────────┬──────────────────────────────────────┬───────────────────────┘

            │  HTTPS (streaming)                   │  MCP over SSE

            ▼                                      ▼

┌────────────────────────────────────────────────────────────────────────────┐

│  DATABRICKS APP — Data Triage Agent  (APX framework)                       │

│                                                                            │

│  ┌───────────────────────────────────────────────────────────────────┐    │

│  │  React Chat UI          FastAPI                                    │    │

│  │  (streaming tokens)  →  POST /invocations   GET /mcp/sse          │    │

│  └─────────────────────────────┬──────────────────────────────────────┘   │

│                                │                                           │

│  ┌─────────────────────────────▼──────────────────────────────────────┐   │

│  │  Claude on FMAI  (FMAPI tool-calling loop)                          │   │

│  │                                                                     │   │

│  │  "Account 12345 not in DR API — investigate"                        │   │

│  │      1. call list_genie_spaces  → see available spaces + IDs        │   │

│  │      2. call query_genie_space  → ask lineage space about acct      │   │

│  │      3. call run_sql_query      → verify data in silver table        │   │

│  │      4. call query_genie_space  → ask jobs space about pipeline      │   │

│  │      5. call read_github_file   → check app-layer filter logic       │   │

│  │      6. synthesize root cause                                        │   │

│  └──────┬──────────────────────────────────────────────────────────────┘   │

│         │  Databricks SDK (OBO token)                                      │

└─────────┼──────────────────────────────────────────────────────────────────┘

          │

          ├─────────────────────────────────────────────────────────┐

          │                                                         │

          ▼                                                         ▼

┌──────────────────────────────────────┐   ┌────────────────────────────────┐

│  DATABRICKS                          │   │  UPLIGHT GENIE SPACES          │

│                                      │   │                                │

│  ┌──────────────┐                    │   │  Discovered at runtime via     │

│  │ SQL Warehouse│                    │   │  ws.genie.list_spaces()        │

│  │ (read-only)  │                    │   │                                │

│  └──────┬───────┘                    │   │  ○ Lineage Space               │

│         │                            │   │    table → upstream sources    │

│  ┌──────▼─────────────────────┐      │   │                                │

│  │ Unity Catalog               │      │   │  ○ Pipeline Jobs Space         │

│  │  bronze / silver / gold    │      │   │    run history, errors         │

│  │  system.access.table_lineage│      │   │                                │

│  └──────┬──────────────────────┘      │   │  ○ Notebooks Space             │

│         │                            │   │    source code references      │

│  ┌──────▼─────────────────────┐      │   │                                │

│  │ Databricks Jobs API         │      │   │  + any new Space auto-         │

│  │  run history / logs        │      │   │    discovered, no code change  │

│  └─────────────────────────────┘      │   └────────────────────────────────┘

└──────────────────────────────────────┘

┌──────────────────────────────────────┐

│  GITHUB (read-only token)            │

│  Pipeline repos + API/app layer repos│

│  (filter logic, transforms)          │

└──────────────────────────────────────┘

**Key design points:**
- **APX exposes two interfaces**: the chat UI calls /invocations (streaming FMAPI loop); MCP clients (Claude Desktop, Cursor) connect to /mcp/sse — same agent, same tools, two access patterns
- **Genie discovery via SDK**: the agent calls ws.genie.list_spaces() at query time to discover available Spaces by title and description, then queries the right one — no Space IDs hardcoded, no separate MCP server needed
- **OBO auth**: Databricks access flows through the invoking user's on-behalf-of token — Unity Catalog permissions are automatically enforced
- **GitHub token**: Read-only, stored in Databricks Secrets, scoped to pipeline + API repos

### **Tool Set**

| **Tool** | **What It Does** |
| --- | --- |
| run_sql_query | Execute read-only SQL against any Databricks table |
| get_table_info | Schema, row count, data freshness for a given table |
| get_table_lineage | Upstream sources via Unity Catalog system.access.table_lineage |
| find_jobs_for_table | Which Databricks jobs write to a given table |
| get_job_run_history | Recent run history — success/failure, timestamps |
| get_job_run_logs | Error output and stack trace from a specific run |
| get_job_notebook_path | Source code paths for the tasks in a job |
| list_genie_spaces | List all available Genie Spaces (title, description, space ID) — called first to discover which spaces exist |
| query_genie_space | Query a specific Genie Space by ID with a natural language question |
| read_github_file | Read a file from a GitHub repo (pipeline or app layer source) |
| search_github_code | Search for patterns (column names, filter conditions) across a repo |

**Note on Genie**: Uplight has already built Genie Spaces covering lineage, pipeline jobs, and notebooks. Rather than replicating that logic with raw SQL tools, the agent discovers and queries those Spaces directly. The list_genie_spaces → query_genie_space pair means any new Space Uplight creates is automatically available to the agent — no code changes needed. **Prerequisite**: Genie Spaces must have meaningful description fields so the agent can pick the right one.

### **Authentication**
- **Databricks access**: Uses on-behalf-of (OBO) token — the agent queries Databricks as the user who invoked it, respecting their existing Unity Catalog permissions
- **GitHub access**: GitHub token stored in Databricks Secrets (read-only, scoped to relevant repos)

———

## **Investigation Flow**

The agent follows a structured investigation pattern for each query:

Step 1: Data presence

  → Does the data exist in the target table?

  → If yes → move to lineage

  → If no → is it in an upstream table?

Step 2: Lineage trace

  → Follow system.access.table_lineage upward

  → Identify where the data drops off

Step 3: Pipeline inspection

  → Find the job that writes the table where data went missing

  → Check recent run history for failures

  → Read the job's source code for filter/drop logic

Step 4: API / app layer inspection (if data reached gold but not the API)

  → Check app layer logic first — filtering may happen in the application, not just the API

  → Search GitHub repo for field/status filters in the app layer

  → Read relevant files for application-level filtering logic

  → This is where GitHub repo access becomes essential

Step 5: Root cause summary

  → Plain-language explanation of where the data was dropped and why

———

## **Phased Approach**

### **Phase 1 — Single Agent, Core Tools (2–3 weeks)**

Deliver a working single-agent APX app with SQL, lineage, and job tools. No GitHub integration yet — covers the majority of "data missing in table" cases.

**Deliverables:**
- APX app deployed to Databricks
- 6 core tools (SQL, lineage, jobs)
- System prompt tuned on 5–10 real triage scenarios
- Basic React chat UI

**Success criteria:** Agent correctly identifies root cause for 3 seed scenarios without human guidance.

### **Phase 2 — GitHub Integration (1–2 weeks)**

Add code-reading tools and tune the agent on cases where data reaches the table but is filtered by application logic.

**Deliverables:**
- 2 additional tools (read_github_file, search_github_code)
- GitHub secret provisioned in Databricks
- Expanded scenario coverage (API-layer filtering)

### **Phase 3 — Multi-Agent Split (optional, future)**

Split into a **Data Agent** (Databricks-only tools) and a **Code Agent** (GitHub tools), orchestrated by a top-level agent. Each sub-agent is independently callable and reusable for other workflows (e.g., a data quality monitoring agent could reuse the Data Agent).

This phase is optional — proceed only if the single-agent approach shows scaling or permission boundary issues.

———

## **Prerequisites**

| **Item** | **Owner** | **Notes** |
| --- | --- | --- |
| system.access.table_lineage enabled | Uplight data eng | Verify lineage is being captured |
| Databricks App environment | Uplight infra | APX framework already in use |
| GitHub read token | Uplight security | Scoped to pipeline + API repos |
| Databricks Secret scope | Uplight infra | For GitHub token storage |
| FMAI access to Claude Sonnet | Databricks | databricks-claude-sonnet-4-6 endpoint |

———

## **What This Is Not**
- **Not a monitoring system.** This agent investigates on-demand; it doesn't proactively alert on data issues. (That's a separate use case.)
- **Not a data quality framework.** It doesn't define SLAs, run scheduled checks, or track historical quality metrics.
- **Not a replacement for lineage tooling.** It uses Unity Catalog lineage as an input; it doesn't replace the lineage system itself.

———

## **Connection to Explain My Bill Prototype**

The Explain My Bill prototype (already built) proves that this pattern works on Uplight's Databricks environment:
- FMAI tool calling works with Claude Sonnet
- The APX agent addon handles the LLM loop, tool dispatch, and streaming
- Workspace-level OAuth (OBO) flows correctly through tool calls

The data triage agent is the same architecture with a different tool set — one targeting the data engineering stack instead of the billing stack.

———

## **Open Questions**
1. Which GitHub repos should the agent have read access to? App layer repos are now confirmed in scope alongside pipeline repos.
2. Should the agent be able to trigger a job re-run, or read-only only?
3. Which Genie Spaces already cover lineage / pipeline jobs / notebooks — can we inventory these to avoid rebuilding that logic?
4. What's the ticket system integration path? (Phase 2 trigger: ticket created → agent auto-investigates → attaches findings to ticket)
5. Should the chat UI be a standalone Databricks App, or embedded in the existing support ticketing tool?
6. Who are the primary users for Phase 1 — support engineers, data engineers, or both?
