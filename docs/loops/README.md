# apx-agent loops

Bounded, repeatable agent loops for the apx-agent lifecycle —
`doctor` → `scaffold` → `deploy` → `eval` → `traces` / `label` / `fleet`.

Each loop below is a feedback system with an explicit trigger, one bounded
action per pass, an observable check, and a named stopping condition. Every
loop is grounded in a real `apx-agent` command surface. Loops that touch
deploys, production, or many agents at once ask before the consequential step;
designing a loop here does **not** enable a schedule or change production —
run one only when you ask.

These match the project's goals: agents that are **grounded** in Unity Catalog
data, **governed** by UC grants and identity passthrough, **observable** through
MLflow traces, and **evaluated** with judges aligned to human experts.

---

## Pre-deploy readiness

Brings a project to a clean `doctor` report before any deploy, fixing one
diagnostic at a time and stopping when green or when a fix stops helping.

Prompt:
> Run `apx-agent doctor`. If it reports a problem, apply the single change its
> `Fix:` line names, re-run `doctor`, and keep the change only if it clears that
> check without breaking another. Repeat for the next remaining problem. Stop
> when `doctor` is all-green, or when a check stops improving across two passes —
> then report what is still failing and why. Ask before editing auth config or
> any file outside the project.

## Eval-guided agent quality

Improves an agent against `eval run` on a working set while gating accepted
changes on a held-out set, so the agent does not overfit its own scorer.

Prompt:
> Split the evalset into a working set and a held-out gate set. Run
> `apx-agent eval run <working>.jsonl`; read the failures and None-prediction
> count. Make one bounded change to instructions or tools, re-run on the working
> set, and keep it only if the score rises. Before accepting a kept change,
> confirm it also holds or improves on `apx-agent eval run <gate>.jsonl`. Stop
> when the gate score meets your target or two passes yield no gate improvement.
> Ask before deploying.

## Judge alignment

Aligns a BYO LLM judge to subject-matter-expert ratings using a labeling
session, repeating only while alignment with the humans keeps improving.

Prompt:
> Run `apx-agent label start --uc-name <cat.sch.agent> --judge <name> --scale 1-5
> --assignee <sme@co>` and share the Review App URL; wait for SMEs to label
> out-of-band. Once labeling is complete, run `apx-agent label align --uc-name
> <cat.sch.agent> --judge <name> --run <run-id>` and compare the judge's
> post-align agreement with the SME ratings against the prior round. Repeat with
> a fresh labeling round only while agreement improves; stop when it meets your
> target or a round adds no agreement. Never overwrite SME labels.

## Grounding freshness

Detects when an agent's baked Unity Catalog schema has drifted from the live
catalog and refreshes the grounding bundle, verifying before any redeploy.

Prompt:
> Run `apx-agent agents refresh-schema` to regenerate the grounding bundle from
> Unity Catalog. If the bundle is unchanged, stop — clean no-op. If it changed,
> review the diff for dropped or renamed tables/columns the agent relies on, then
> confirm the agent still answers its core questions with `apx-agent eval run`.
> Keep the refreshed bundle only if eval holds. Stop after one refresh per run.
> Ask before redeploying the updated grounding to a live agent.

## Trace regression triage

Watches an agent's recent MLflow traces for failures or empty predictions,
fixing one root cause per pass and verifying the fix before moving on.

Prompt:
> Run `apx-agent traces list --agent <name>` and find traces with errors,
> tool-call failures, or None predictions newer than the last pass. Pick the most
> frequent failure, identify its root cause, and make one bounded fix. Verify by
> re-running the failing case through `apx-agent eval run` (or a targeted
> reproduction) and confirm it now passes without regressing others. Record the
> trace id, cause, and fix. Stop when no new failing traces remain or a cause
> needs a change larger than one fix — then escalate it. Ask before deploying.

## Fleet drift remediation

Brings a tagged set of deployed agents back to a target configuration with
`fleet`, always previewing as a dry-run and requiring approval before applying.

Prompt:
> Run `apx-agent fleet list --where <selector>` to find agents off the target
> config (missing tags, stale build, un-backfilled). Preview the remediation as a
> dry-run first. Show the exact set of agents and the change, then ask for
> approval before applying `tag` / `backfill` / `redeploy` for real. Apply to one
> batch, re-run `fleet list` to confirm those agents now match, and continue to
> the next batch. Stop when the selector returns no drifted agents, or a batch
> fails to converge — then report which agents and why.
