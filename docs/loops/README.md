# apx-agent loops

Bounded, repeatable agent loops for two halves of apx-agent's life: the
**operational lifecycle** of deployed agents, and the **engineering discipline**
of changing apx-agent itself.

Each loop below is a feedback system with an explicit trigger, one bounded
action per pass, an observable check, and a named stopping condition. Every
loop is grounded in a real command surface. Loops that touch deploys,
production, or many agents at once ask before the consequential step; designing
a loop here does **not** enable a schedule or change production — run one only
when you ask.

Two disciplines run through every loop and are what make looping safe as the
connective tissue for continuous improvement:

- **Ponytail (don't over-engineer).** apx-agent's thesis is *declared, not
  wired*: a working agent is one Python object or a `[tool.apx.agent]` block.
  Every change picks the smallest approach that works and prunes anything not
  load-bearing — the loop's "choose" step prefers reusing an existing primitive
  over adding code.
- **Ctk (claim-vs-reality, read-after-write).** A pass is not "done" because a
  command exited 0. The repo's vendored `ctk` kit (`python/.ctk`) exists to
  catch work that *claims success but didn't do the thing* — empty output,
  swallowed exceptions, unvalidated artifacts. The loop's "verify" step reads
  the result back (`ctk.verify(Artifact(...))`, a `*_reality_ctk.py` test, a
  re-read trace) and only then records progress.

---

## Operational lifecycle loops

For deployed agents — keeping them **grounded** in Unity Catalog data,
**governed** by UC grants, **observable** through MLflow traces, and
**evaluated** with judges aligned to human experts.

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

---

## Engineering-discipline loops

For changing apx-agent itself — these are the continuous-improvement loops that
bake **Ponytail** (don't over-engineer) and **Ctk** (read-after-write
verification) into the coding workflow. They run in `python/`, where
`ctk` is on the path via `pythonpath = [".ctk"]`.

## Read-after-write change (Ctk)

Closes the loop on a single code change by reading the result back before
calling it done, so an exit-0-with-empty-output never passes as success.

Prompt:
> Make one bounded change. Before claiming it works, read the result back:
> `cd python && uv run pytest` for the affected area, and for any artifact the
> change produces or modifies, assert it is real — non-empty AND carrying the
> wiring that makes it work — with `ctk.verify(Artifact(...))` or a
> `*_reality_ctk.py` test, not just `.exists()`. Keep the change only if the
> read-back passes. Stop when the reality check is green; if it can't pass in two
> attempts, revert and report the gap. Never report a passing exit code as
> success without the read-back.

## Simplicity guard (Ponytail)

Makes a change land at the smallest footprint that works, reusing existing
primitives over new code and pruning anything not load-bearing.

Prompt:
> Before writing code, search for an existing apx-agent primitive, helper, or
> declared option that already does the job (`declared, not wired` is the
> default). Implement the smallest version that works, then remove any
> abstraction, flag, or branch the tests don't exercise. Verify the simpler
> version still passes `cd python && uv run pytest`. Stop when no further removal
> keeps tests green. Ask before deleting code outside the change's scope.

## Reality-test backfill

Raises claim-vs-reality coverage one feature at a time, adding a real read-back
test wherever a behaviour is only checked for existence.

Prompt:
> Find one CLI command or generated artifact covered only by an `.exists()` /
> exit-code check. Add a `*_reality_ctk.py` test that reads the output back and
> asserts it is real with `ctk.verify(Artifact(..., min_bytes=..., must_contain=...))`.
> Confirm the new test passes and fails when the artifact is emptied. Record the
> feature covered. Stop when no existence-only feature remains, or the next one
> needs a fixture beyond the kit — then note it. Don't weaken the assertion to
> make it pass.

## Swallowed-exception sweep

Drives the codebase toward zero error-hiding `except` blocks, fixing one real
finding per pass with a verified read-back.

Prompt:
> Run the static scan — `from ctk import find_swallowed_exceptions` over
> `src/apx_agent/` (or the area in scope). For the first finding, decide whether
> the handler should re-raise, surface the error, or is a justified no-op; apply
> the smallest fix (Ponytail — don't restructure surrounding code). Verify with
> `cd python && uv run pytest` and re-run the scan to confirm that finding is
> gone without adding others. Stop when the scan returns empty or a remaining
> finding needs an owner decision — then escalate it.

## Doc-claims-vs-code drift

Keeps the docs honest by verifying each command or flag a doc claims actually
exists, fixing one drift per pass.

Prompt:
> Pick a doc page that claims a command, flag, or behaviour (e.g. an
> `apx-agent ...` invocation). For one claim, verify it against the code —
> `apx-agent --help`, the CLI source, or a quick run. If it drifted, make the
> smallest correction to the doc (or the code, if the doc is the intended
> contract) and confirm the corrected claim now holds by running it. Record the
> page and claim. Stop when the page's claims all hold, or a drift needs a
> product decision — then flag it. Never edit a doc to match without checking the
> code first.
