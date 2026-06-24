# Development loops for apx-agent

Bounded, repeatable loops for **developing apx-agent itself** — the
continuous-improvement cycles a contributor (human or coding agent) runs while
changing this repo. They are not loops for end users' deployed agents; they are
how we keep apx-agent correct, small, and honest as it grows.

Each loop is a feedback system with an explicit trigger, one bounded action per
pass, an **observable check on a real repo gate**, and a named stopping
condition. The gates are this repo's actual ones:

- `cd python && uv run pytest` — the full suite (2600+ tests), including the
  `*_reality_ctk.py` claim-vs-reality tests.
- `make check` — the one-command verify gate (runs the suite).
- `pre-commit run --all-files` — the lint suite in `.pre-commit-config.yaml`
  (no swallowed exceptions, no `.get(k, "")`, no invented env defaults, no
  skipped tests, …).
- `ctk` (`python/.ctk`) — `ctk.verify(Artifact(...))`, `find_swallowed_exceptions`.

## Two disciplines run through every loop

These are what make looping safe as the connective tissue for continuous
improvement — they keep the cycle from drifting into over-build or false
"done":

- **Ponytail (don't over-engineer).** apx-agent's thesis is *declared, not
  wired*. Every change picks the smallest approach that works and prunes
  anything not load-bearing. The "choose" step prefers reusing an existing
  primitive over adding code; the lint suite enforces several of these smells.
- **Ctk (claim-vs-reality, read-after-write).** A pass is not "done" because a
  command exited 0. The vendored `ctk` kit exists to catch work that *claims
  success but didn't do the thing* — empty output, swallowed exceptions,
  unvalidated artifacts. The "verify" step reads the result back before
  recording progress.

See [CLAUDE.md](../../CLAUDE.md) for the short version every coding session gets.

---

## Read-after-write change (Ctk)

Closes the loop on one code change by reading the result back before calling it
done, so an exit-0-with-empty-output never passes as success.

Prompt:
> Make one bounded change. Before claiming it works, read the result back: run
> `make check` (or `cd python && uv run pytest` for the affected area), and for
> any artifact the change produces, assert it is real — non-empty AND carrying
> the wiring that makes it work — with `ctk.verify(Artifact(...))` or a
> `*_reality_ctk.py` test, not just `.exists()`. Keep the change only if the
> read-back passes. Stop when the reality check is green; if it can't pass in two
> attempts, revert and report the gap. Never report a green exit code as success
> without the read-back.

## Simplicity guard (Ponytail)

Makes a change land at the smallest footprint that works, reusing existing
primitives over new code and pruning anything the tests don't exercise.

Prompt:
> Before writing code, search for an existing apx-agent primitive, helper, or
> declared option that already does the job. Implement the smallest version that
> works, then remove any abstraction, flag, or branch the tests don't exercise.
> Verify the simpler version still passes `make check` and `pre-commit run
> --all-files`. Stop when no further removal keeps both green. Ask before
> deleting code outside the change's scope.

## Green-CI-before-merge

Drives a branch to a fully green local gate before it goes up for review, fixing
one failure class per pass.

Prompt:
> Run `make check` and `pre-commit run --all-files`. For the first failure,
> diagnose the root cause and apply the smallest fix (Ponytail — don't
> restructure unrelated code). Re-run both gates and confirm that failure is gone
> without introducing another. Repeat for the next failure. Stop when both gates
> are green, or a failure needs a decision beyond a local fix — then report it
> rather than weakening or skipping the test.

## Reality-test backfill (Ctk)

Raises claim-vs-reality coverage one feature at a time, wherever a behaviour is
only checked for existence.

Prompt:
> Find one CLI command or generated artifact covered only by an `.exists()` /
> exit-code check. Add a `*_reality_ctk.py` test that reads the output back and
> asserts it is real with `ctk.verify(Artifact(..., min_bytes=..., must_contain=...))`.
> Confirm the test passes, then confirm it *fails* when the artifact is emptied —
> a test that can't fail proves nothing. Record the feature covered. Stop when no
> existence-only feature remains, or the next needs a fixture beyond the kit —
> then note it. Don't weaken the assertion to make it pass.

## Swallowed-exception sweep

Keeps newly changed code free of error-hiding `except` blocks, fixing one real
finding per pass.

Prompt:
> Run `find_swallowed_exceptions` over the files your change touched (the whole
> tree still has justified log-only handlers, so scope to the diff). For the
> first finding, decide whether the handler should re-raise, surface the error,
> or is a justified no-op; apply the smallest fix. Verify with `make check` and
> re-run the scan to confirm that finding is gone without adding others. Stop when
> the scoped scan is clean or a finding needs an owner decision — then escalate.

## Doc-claims-vs-code drift (Ctk)

Keeps the docs honest by verifying each command or flag a doc claims actually
exists, fixing one drift per pass.

Prompt:
> Pick a doc page that claims a command, flag, or behaviour (e.g. an
> `apx-agent …` invocation). For one claim, verify it against the code —
> `apx-agent --help`, the CLI source, or a quick run. If it drifted, make the
> smallest correction to the doc (or the code, if the doc is the intended
> contract) and confirm the corrected claim now holds by running it. Record the
> page and claim. Stop when the page's claims all hold, or a drift needs a product
> decision — then flag it. Never edit a doc to match without checking the code
> first.

---

These loops are also available as reusable scaffolds in the loop-library skill:
[`.claude/skills/loop-library/references/examples.md`](../../.claude/skills/loop-library/references/examples.md).
