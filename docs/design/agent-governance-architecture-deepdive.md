# Agent Governance — Architecture Deep-Dive

**Status:** thought-leadership / reference · **Date:** 2026-09-28

A narrative deep-dive companion to
[`agent-governance-for-uc-data-architectures.md`](agent-governance-for-uc-data-architectures.md).
Each section is a "slide" — a single idea, a diagram in words, and the talk
track. It walks an at-scale Unity Catalog data architecture layer by layer and
shows how each layer extends to agents. Read top to bottom for a customer
conversation; jump to a layer for a targeted answer.

---

## 1. Where we started: governing access to data

> **Talk track:** "You've built this. Let's name it before we extend it."

The mature UC architecture governs *which principal reads which data, under what
filters*:

```
        Account (identities, metastores)
              │
        Metastore  ──────────────  system.access.audit  (who did what)
              │
     Catalog (LOB / domain)         RLS / CLS / masks   (fine-grained)
        │        │
     Schema     Schema              Storage creds /      (physical isolation)
        │        │                  external locations
     Table    Volume
```

Two actors move through it: **users** (interactive, own grants) and **service
principals** (fixed non-human identities — pipelines, CI/CD). Everything is a
grant on a securable in a three-level namespace, owned by a group, audited in
system tables.

---

## 2. What agents change: the principal is new, the model is not

> **Talk track:** "An agent is the first actor that is *both* a service principal
> and a stand-in for a user. That's the only thing that's new."

An agent is a non-human identity (like an SP) that acts with a human's intent and
often on the human's behalf (like a user). So the access question gains a
qualifier:

```
  Classic:   principal ──grant──> securable
  Agent:     agent (identity?) ──acting for──> user (whose grants?) ──> securable
```

The design decision at every tool call: **whose UC grants gate this operation —
the agent's, or the calling user's?**

---

## 3. The three identity patterns

> **Talk track:** "Three patterns. You pick per tool, not per agent — that's the
> unlock."

```
  ┌─ Agent as its own SP ────  agent's grants gate data
  │     use: shared access, unattended/batch, the agent's own reach
  │
  ├─ On-behalf-of (OBO) ──────  the CALLING USER's grants gate data
  │     use: sensitive/regulated data — RLS/CLS/masks apply per-user
  │
  └─ Hybrid ─────────────────  SP for some tools, OBO for others
        use: one workflow, mixed human-gated and machine steps
```

In apx-agent this is `ExecutionIdentity = "user" | "service"` **declared on the
tool**, enforced at compile time and runtime. The architecture choice is a
one-word declaration, not wiring — and the deploy reads it back.

---

## 4. Layer by layer: the agent slots in

> **Talk track:** "Walk your existing governance layers. The agent extends each
> one — it doesn't replace any."

**Isolation hierarchy / taxonomy** → the agent is registered *in* Unity Catalog,
in a `catalog.schema`, granted with the normal model (`EXECUTE` to run it). "What
agents exist, who owns them, who may run them" = your existing namespace + grants.
An agent is a data product: owner, domain, contract.

**Operating model** → existing admin/owner roles hold; add *who provisions,
rotates, revokes the agent's SP*. Existing best practice extends directly — OWNER
is a group, audit SPs regularly — but at fleet volume, so it can't be manual.

**RLS / CLS / ABAC** → the payoff: **OBO means your existing row filters and
column masks apply to the agent's queries automatically.** No separate agent-ACL
system. Human governance = agent governance.

**Scopes / least privilege** → the platform downscopes the agent's OBO token to
its declared `user_api_scopes`; the agent can never exceed what it declares. apx
declares those scopes so least privilege is a deploy-time property.

**Audit / system tables** → **dual attribution**: the log shows *the agent acted*
AND *who triggered it*. A column of nuance on the observability you already run.

**Data protection** → masking + OBO scoping is the containment against an actor
that moves faster than a human. Same techniques, higher stakes.

---

## 5. At-scale: agent-to-agent trust

> **Talk track:** "When agents call agents, the RLS you designed still has to
> bite three hops deep."

```
   user ──token──> Agent A ──carries user──> Agent B ──carries user──> data
                                                         (user's RLS/CLS applies)
```

apx forwards the caller's user identity **per hop** (`X-Forwarded-Access-Token`),
so a multi-agent chain runs under the *user's* grants end to end — not a shared
broad SP. RLS/CLS enforcement survives the whole chain. Cross-agent trust hung on
SP grants alone is a per-environment detail that breaks in a real fleet; the
user's identity carried forward does not.

---

## 6. The close

> **Talk track:** "This isn't a new governance project. It's the governance you
> own, extended to a new principal type."

An agent is:

1. a **governed identity** (SP, provisioned/audited like a user),
2. that **carries the caller's intent** (OBO → your RLS/CLS still applies),
3. and is a **cataloged, grantable securable** (same grant model as a table).

apx-agent makes all three **declared, not wired** — so an at-scale customer's
Unity Catalog governance becomes their agent governance for free, from one agent
to a fleet. That is the thought-leadership message: *you already know how to do
this; agents are the same architecture, one principal wider.*
