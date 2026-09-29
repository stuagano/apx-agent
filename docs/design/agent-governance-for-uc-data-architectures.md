# Agent Governance for Unity Catalog Data Architectures

**Status:** thought-leadership / reference · **Date:** 2026-09-28

At-scale Databricks customers have spent years building a Unity Catalog data
architecture: an isolation hierarchy of catalogs and schemas, data products with
owners and contracts, an operating model of account / metastore / workspace
admins and data owners, row- and column-level security, and end-to-end audit via
system tables. That model governs **access to data** — it answers *"which
principal may read which table, and under what filters."*

Agents introduce a question that model was never asked: **who is the agent, and
whose grants apply when it acts on data?** This document argues the answer is not
a parallel governance system. It is the **same Unity Catalog primitives, extended
to a new principal type.** apx-agent's job is to make that extension *declared,
not hand-wired* — the same thesis apx applies everywhere.

---

## The shift: a third principal type

A classic UC architecture has two kinds of actor:

- **Users** — interactive, carry their own identity and grants.
- **Service principals** — a fixed, known non-human identity doing a fixed job
  (a pipeline, CI/CD). Governed like a user, but there is no "calling human."

An agent is **both at once.** It is a non-human identity (like an SP) but it acts
with a human's intent and, for sensitive data, often *on that human's behalf*
(like a user). Every access decision in the old model now has to answer: *when
this agent touches data, whose UC grants gate it?*

That collapses to three patterns. Choosing per-tool is the whole game.

| Pattern | Whose UC grants gate the data | When to use |
|---|---|---|
| **Agent as its own identity** (service principal) | the agent's own grants | consistent access for all users; unattended/batch; the agent's own reach |
| **On-behalf-of user (OBO)** | the *calling user's* grants — RLS / CLS / masks apply per-user | the agent must respect who is asking (the default for regulated / sensitive data) |
| **Hybrid** | agent SP for some tools, OBO for others | one workflow with mixed steps — where real agents land |

In apx-agent this is a **declaration on the tool**, not plumbing:
`ExecutionIdentity` is `"user"` (OBO) or `"service"` (agent identity), decided
per tool and enforced at compile time and runtime (`_tool_scope.py`, PR #758).
The architecture decision ("whose grants gate this operation?") becomes a
one-word declaration the deploy reads back.

---

## Mapping onto the existing UC architecture

The point worth making to an at-scale customer: **your best practices do not get
thrown out. The agent slots into them.**

### Isolation hierarchy & catalog taxonomy

An agent is governed like any other securable. The UC direction is to **register
the agent itself in Unity Catalog** — in a `catalog.schema`, granted with the
same model that protects tables and functions (`EXECUTE` to run it). "Which
agents exist, who owns them, who may run them" is answered by the **same
three-level namespace and grant model** the customer already runs. An agent
becomes a data product: an owner, a domain, a contract — exactly the data-product
framing an at-scale UC design already teaches.

### Operating model (admin & ownership roles)

The existing roles hold; agents add an **identity-lifecycle** concern: *who
provisions, rotates, and revokes the agent's service principal?* The existing
best practices extend directly — **set OWNER to a group, not a user; audit
service principals regularly** — but now at fleet volume, so it cannot be manual.
Governing a hundred agents is governing a hundred non-human identities; the
operating model is the same shape, applied to more principals.

### Fine-grained access (RLS / CLS / ABAC)

This is the payoff, and the line that makes a security team comfortable: **if the
agent runs OBO, the row filters and column masks the customer already designed
apply automatically to the agent's queries.** There is no separate agent-ACL
system to build. The governance designed for humans *is* the governance for
agents. apx forwards the caller's identity per hop (`X-Forwarded-Access-Token`)
so the downstream data access is the user's, filters and all.

### Scopes and least privilege

The Databricks Apps platform exchanges a user's token for one **downscoped to the
app's effective `user_api_scopes`** — the app can never exceed the scopes it
declares. apx **declares** the scopes its tools need (derived from resources, or
stated explicitly) so least privilege is a deploy-time property, not a runtime
surprise. This is the same "principle of least privilege on the app's OAuth
scopes" the UC operating model already calls for — made declarative.

### Audit & observability (system tables)

The new requirement is **dual attribution**: the audit trail must record that
*the agent acted* **and** *which user or schedule triggered it*. Automated agent
activity stays distinguishable from human activity, while the triggering
principal is logged alongside. This is not a new audit system — it is a column of
nuance on the `system.access.audit` observability the architecture already
depends on.

### Data protection (masking / tokenization / encryption)

Unchanged in mechanism, higher in stakes: an agent can move through data faster
than a human can. Column masking plus OBO scoping is the containment — the same
techniques, now load-bearing against a faster actor.

---

## Agent-to-agent: the trust boundary at scale

When one agent calls another (a supervisor calling a sub-agent, or a fleet of
cooperating agents), the governance question compounds: *on which hop, as whom?*

apx's answer is **per-hop user OBO forwarding**: the caller's user identity is
carried to the callee, and the downstream hop runs under the *user's* UC grants —
not a shared, broad service principal. This keeps RLS/CLS enforcement intact
across the whole multi-agent chain, and it holds regardless of the underlying
service-principal topology. Hanging cross-agent trust on service-principal grants
alone is a per-environment detail that does not survive contact with a real
fleet; the user's identity, carried forward, does.

---

## The mental model in one line

> The classic model governs **access to data.** The agent era adds governing
> **the actor that acts on data** — and the answer is the *same* Unity Catalog
> primitives extended to a new principal type, not a parallel system.

Concretely, an agent is:

1. **a governed identity** — a service principal, provisioned and audited like a
   user;
2. **that carries the caller's intent** — via OBO, so the row filters and column
   masks already in place still bite;
3. **and is itself a cataloged, grantable securable** — discovered and permitted
   with the same grant model as a table.

apx-agent's contribution is to make all three **declared, not wired**: identity
per tool, scopes derived and least-privilege, the caller carried per hop — so a
customer's existing Unity Catalog governance becomes their agent governance for
free, from one agent to a fleet.

---

## For customer conversations

This is the deep-dive to reach for when an at-scale UC customer asks "how do we
govern the agents?" — positioned as an extension of the governance design they
already own, not a new project. The companion narrative walkthrough lives in
[`agent-governance-architecture-deepdive.md`](agent-governance-architecture-deepdive.md).

See also: [`served-path-guards-and-identity.md`](served-path-guards-and-identity.md)
(how apx resolves identity on served paths).
