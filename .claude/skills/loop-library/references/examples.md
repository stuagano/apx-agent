# Reference example loops

Worked examples of well-formed loops to use as scaffolds when designing or
adapting. They show the delivery format and the shape of a sound feedback cycle.

**These are reference templates, not published Loop Library entries.** Do not
present them as published, and do not invent a catalog title, number, or URL for
them. When one fits, adapt it — replace its thresholds, tools, gates, and
stopping condition with the user's grounded details — and label the result as an
adaptation.

Each example states the loop's purpose in one sentence, then the copy-ready
prompt. The internal cycle (observe → choose → act → verify → record → repeat
or stop) is implicit in every prompt; keep it that way in delivery — do not
print the six-step schema unless the user asks for an audit.

---

## Pattern: read-after-write change (verify a claim before calling it done)

Closes the loop on one change by reading the result back, so an
exit-0-with-empty-output never passes as success.

Prompt:
> Make one bounded change. Before claiming it works, read the result back: run
> the project's test/verify gate, and for any artifact the change produces,
> assert it is real — non-empty AND carrying the wiring that makes it work — not
> just that the file exists. Keep the change only if the read-back passes. Stop
> when the check is green; if it can't pass in two attempts, revert and report
> the gap. Never report a green exit code as success without the read-back.

When to reach for it: any task where the agent could plausibly claim success
without having done the thing (codegen, file writes, migrations, fixes).

## Pattern: simplicity guard (don't over-engineer)

Makes a change land at the smallest footprint that works, reusing what exists
over adding new code.

Prompt:
> Before writing code, search for an existing primitive, helper, or option that
> already does the job. Implement the smallest version that works, then remove
> any abstraction, flag, or branch the tests don't exercise. Verify the simpler
> version still passes the test and lint gates. Stop when no further removal
> keeps them green. Ask before deleting code outside the change's scope.

When to reach for it: codebases prone to speculative generality; pairs well with
the read-after-write loop as a single development cycle.

## Pattern: green-gate-before-handoff

Drives a branch to a fully green local gate before review, fixing one failure
class per pass.

Prompt:
> Run the test and lint gates. For the first failure, diagnose the root cause and
> apply the smallest fix without restructuring unrelated code. Re-run both gates
> and confirm that failure is gone without introducing another. Repeat for the
> next failure. Stop when both gates are green, or a failure needs a decision
> beyond a local fix — then report it rather than weakening or skipping the test.

When to reach for it: pre-PR cleanup, CI-red triage. Terminal state is green
gate or an escalated blocker — never "skipped the test to get green".

## Pattern: coverage backfill (one verified test per pass)

Raises real, claim-vs-reality test coverage one behaviour at a time.

Prompt:
> Find one command or generated artifact covered only by an existence / exit-code
> check. Add a test that reads the output back and asserts it is real (non-empty
> and contains the expected wiring). Confirm the test passes, then confirm it
> *fails* when the artifact is emptied — a test that can't fail proves nothing.
> Record the behaviour covered. Stop when no existence-only behaviour remains, or
> the next needs a fixture you don't have — then note it. Don't weaken the
> assertion to make it pass.

When to reach for it: hardening a suite that asserts presence but not content.

## Pattern: doc-claims-vs-code drift

Keeps docs honest by verifying each claim against the code, one drift per pass.

Prompt:
> Pick a doc page that claims a command, flag, or behaviour. For one claim,
> verify it against the code — the help output, the source, or a quick run. If it
> drifted, make the smallest correction to the doc (or the code, if the doc is
> the intended contract) and confirm the corrected claim now holds by running it.
> Record the page and claim. Stop when the page's claims all hold, or a drift
> needs a product decision — then flag it. Never edit a doc to match without
> checking the code first.

When to reach for it: docs that have drifted from a fast-moving CLI or API.

---

These five are grounded, runnable instances in this repo's development workflow —
see [`docs/loops/README.md`](../../../../docs/loops/README.md), where the gates
are `make check`, `pre-commit run --all-files`, and the `ctk` kit's
`ctk.verify(Artifact(...))` / `find_swallowed_exceptions`. Use them as the
concrete reference when adapting any of the patterns above to a project.
