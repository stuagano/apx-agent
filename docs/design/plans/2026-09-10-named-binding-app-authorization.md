# Named binding Apps authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a finalized, declared A2A named binding to a Databricks App contribute the same governed native App dependency and `CAN_USE` authorization plan as a legacy URL-based sub-agent declaration.

**Architecture:** Reuse the existing deploy-time authorization compiler and `AppDependency` set. The finalized root already owns a private immutable map of logical binding names to validated locations. Add its Databricks Apps locations to the existing dependency collection after normalizing an optional `/.well-known/agent.json` card path to the base App URL. Let the existing exact workspace resolver and native resource reconciliation continue to establish immutable App identity and `CAN_USE`; do not add a public binding field, App-ID model, card fetch, or second authorization path.

**Tech Stack:** Python 3.11+, existing Apps authorization compiler, pytest, frozen `uv` test gate. No live workspace, deployment, profile selection, or network calls.

**Spec:** [Declarative role pipelines](../declarative-role-pipelines.md)

## Global Constraints

- Consume only finalized private `_apx_remote_leaf_bindings`; never expose binding URLs, `_RemoteLeafBinding`, or a new public compiler parameter.
- Only accepted Databricks Apps HTTPS locations contribute native App dependencies. Arbitrary external A2A URLs remain transport-only and must not create resources.
- Preserve explicit-profile and exact workspace URL-to-App resolution. Do not infer App name from hostname, card metadata, or caller-provided identifiers.
- Normalize only the new binding locations from a full agent-card URL to the App base URL. Do not change legacy sub-agent URL behavior in this task.
- Merge into the existing `set[AppDependency]` so legacy and named declarations of the same peer deduplicate.
- Test with generic names and fake URLs; do not deploy or select a Databricks profile.

## File map

- Modify: `python/src/apx_agent/_apps_authorization.py` — add private finalized-binding dependency collection at the current authorization convergence point.
- Modify: `python/tests/test_apps_authorization.py` — prove base/card URL normalization, external-peer exclusion, and legacy/binding deduplication.
- Modify only if required for a real bundle readback proof: `python/tests/test_deploy_apps.py` — prove finalized binding becomes a native App resource with `CAN_USE` through existing fake-workspace deployment seams.
- Update: `docs/multi-agent/a2a.md` only after the code proof is green, so the authorization statement matches shipped behavior.

---

### Task 1: Compile finalized named bindings into native App authorization

**Files:**
- Modify: `python/src/apx_agent/_apps_authorization.py`
- Modify: `python/tests/test_apps_authorization.py`
- Modify only if needed: `python/tests/test_deploy_apps.py`
- Modify after proof: `docs/multi-agent/a2a.md`

**Interfaces:**
- Consumes: finalized root private binding map and existing Apps-host URL validation.
- Produces: existing `AppDependency` records, resolved by the existing deploy compiler into existing native App `CAN_USE` resources.

- [ ] **Step 1: Write focused authorization-plan regressions first.**

Create a finalized supported graph with a generic named leaf binding. Parameterize a Databricks Apps base URL and a full `/.well-known/agent.json` card URL. Compile the authorization plan and assert exactly one dependency with the base App URL. Add a legacy sub-agent declaration for the same peer and assert the set still has one dependency. Add an ordinary external HTTPS binding and assert it does not create a native App dependency.

Run the focused test before implementation and record its genuine failure.

- [ ] **Step 2: Add the smallest private dependency collector.**

At the existing authorization-plan convergence point, read `getattr(agent, "_apx_remote_leaf_bindings", {})`, normalize only an agent-card suffix to a base App URL, reuse the existing hostname-aware Apps URL predicate, and add an ordinary `AppDependency` to the existing set. Do not modify the public model, runtime remote transport, workspace resolver, or bundle resource logic.

- [ ] **Step 3: Prove the real local deployment readback if the existing focused harness makes it small.**

Use the existing fake workspace/profile deployment test seam to load a finalized agent with an environment-backed named binding and a full card URL. Read generated `databricks.yml` and prove one native App resource with the resolved App name and `CAN_USE`; prove absent or ambiguous App resolution fails before bundle deployment. If the existing authorization-plan test already drives the same integration seam and a separate deployment test would duplicate it, explain the precise coverage in the task report.

- [ ] **Step 4: Run focused regressions and correct the public authorization sentence.**

Run the authorization, deploy, binding, and generator regressions under frozen `uv`; run targeted lint and `git diff --check`. Only after the code proof is green, revise the affected A2A documentation sentence to state automatic native App authorization accurately for declared bindings. Do not add a transport class, raw card URL, or customer-specific material to docs.

- [ ] **Step 5: Commit the narrow change and record evidence.**

Commit the smallest code/test/doc scope. The report must include exact RED/GREEN commands, normalization/deduplication/external exclusion evidence, any deployment readback, and confirm no live profile or deploy was used.
