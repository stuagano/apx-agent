# Authoritative output completion: local validation

## Scope and state

- Owner: `authoritative-tool-completion`, branch `stuartg-dev/authoritative-tool-completion`.
- Intentional exact-deployed base: `0541b1c3d57a5a4d345056aab2b85e9f78732bd7`.
- Implementation and validation were authorized locally. After reviewing the results,
  the user authorized committing and pushing this branch. Merge, package publication,
  deployment, hosted invocation, permission changes, and timeout increases remain outside scope.
- Original Curinos checkout/environment/evidence and unrelated APX worktrees preserved.
- Handoff context: [HANDOFF-RECEIPT.md](HANDOFF-RECEIPT.md).

## Change

An invocation-scoped middleware boundary records the input message count. After the
tool batch completes, the next model-entry hook ends the inner graph if this invocation
called the declared output tool. The existing outer wrapper still rejects missing,
duplicate, failed, malformed, or guardrail-rejected evidence before success publication.
It appends a fresh deterministic terminal AI message instead of repurposing a model
generation. Actual model/tool messages and model usage remain intact; the terminal
message has no invented provider response metadata or usage.

The gate handles mixed tool batches, permits prerequisite helper rounds, and stores
its invocation boundary in graph state rather than shared mutable closure state.
Explicit iteration limits account for the additional graph nodes. Ordinary agents
keep their original middleware and iteration calculation. Loop/handoff members reject
output schemas already, so they cannot accept the required output-tool declaration.

## Red-first evidence

1. Before production edits, real compiled graph tests for sync, async, and streaming
   failed with `assert 2 == 1` model calls (three failures).
2. The first direct-return candidate passed single-output-tool cases but failed mixed
   output/helper batches: three failures and three passes. Independent review also
   reproduced the installed LangChain routing rule: all tools must be direct-return.
3. Replacing that candidate with the middleware gate passed all six cases.
4. The explicit one-iteration test exposed insufficient graph-step allowance; the
   corrected calculation passed. No wall-clock timeout was changed.

## Verification

- `UV_OFFLINE=1 make check`: **5,372 passed, 13 skipped, 246 warnings**, exit 0;
  both public-PyPI lockfile checks passed. [Log](.build/apx-check.log).
- Final focused checks: **16 passed**. Includes sync/async/stream, mixed batches,
  prior history, tool state writes, exact output, honest usage/IDs, paired callback
  IDs, one completion callback, missing/duplicate/error/schema/guardrail/denied
  results, ordinary agent behavior, native typed ASK interruption, prerequisite
  helper rounds, async tools, and concurrent invocation isolation.
  The concurrency case was added after full-suite collection and verified separately.
  [Log](.build/focused-complete.log).
- Same behavior tests under the copied Curinos dependencies (LangChain 1.4.6 versus
  APX lock's 1.2.18): **14 passed**, with two pytest configuration warnings because
  that environment lacks pytest-timeout. [Log](.build/apx-curinos-deps-final.log).
- Local lint hooks and `git diff --check` passed. [Lint log](.build/lint-complete.log).
- Targeted Pyright reports **three pre-existing errors**: tool content's string/list
  union at guardrail/JSON boundaries and optional final-message access. An isolated
  copy of the exact base, checked with the same interpreter, reproduces the same
  three errors. No new error was introduced; this is not a clean type-check claim.
  [Current](.build/pyright.log), [baseline](.build/pyright-baseline-resolved.log).

## Independent review

A separate read-only reviewer found the mixed-batch defect in the initial candidate.
After its replacement, the reviewer reported no remaining concrete correctness or
security findings. Review covered invocation isolation/history, missing execution,
validation and governance boundaries, model metadata, checkpoints, typed ASK, and
ordinary-agent behavior. No cross-vendor gateway invocation was attempted because
this task prohibits remote calls.

## Curinos regression

The reviewed environment and application source were copied into `.build/` here.
The original environment and source were not edited. Explicit APX source resolution
was verified before tests. Broad bare-pytest discovery also collected the unrelated
`proc-converter` project and stopped with nine import errors; the intended application
command is `python -m pytest tests -q`.

The first application run returned **943 passed, 5 failed**:

- `test_bundle_declares_read_only_typed_a2a_targets`,
  `test_tracked_requirements_lock_pins_the_exact_apx_agent_revision`, and
  `test_bundle_has_only_governed_read_model_permissions_and_passes_local_verification`:
  copied lockfile was not tracked in the enclosing APX Git repository.
- `NativeInstallerTests::test_stale_environment_repaired_even_when_requirements_unchanged`:
  inherited `PYTHONPATH` made the deliberately minimal installer fixture import full
  APX source instead of its synthetic wheel, causing missing `fastapi`.
- `test_actual_calibrate_graph_preserves_tool_result_and_strict_input[True]`:
  expected three model/tool callback pairs, observed the intended two.

The corrected harness installs this checkout as editable APX in the copied environment
and unsets `PYTHONPATH`, so subprocesses also use modified APX while synthetic installer
environments retain their own packages. The source snapshot is `.build/curinos-one-agent`.
Read-only `GIT_DIR`/`GIT_WORK_TREE` mapping to the original Curinos index preserves
lockfile tracking verification for that copied source; `GIT_OPTIONAL_LOCKS=0` is set.
No Git index, branch, source file, or original dependency environment is modified.

[curinos-authoritative-timing.patch](curinos-authoritative-timing.patch) is applied only
to the test copy. It changes the expected pair count from three to two and additionally
asserts exactly one model start and one tool start. The source Curinos test remains
unchanged; this patch must accompany later integration of the APX fix.

The corrected full application run passed: **948 passed, 1 dependency warning**,
exit 0, in 165.49 seconds. [Final log](.build/curinos-final.log).
The original test was not changed, and `git apply --check` verified that the supplied
patch applies cleanly to the original Curinos checkout without applying it.
The original reviewed environment's installed `_compile.py` still matches the exact
base byte-for-byte (SHA-256 `d1c3fb3df018821bd421dd5d6227db92762c7000a4539379cea787f0dd6c6afb`).

Implementation and local validation are complete. The three APX code/test files and
handoff/report/Curinos patch are included in the authorized branch commit and push.
The handoff receipt records the original local-only authorization; the subsequent
user instruction to push supersedes that limit for this branch. Retain this checkout
for its local validation logs and isolated regression fixtures; no PR or merge is part
of this push.

## Proof limits

Local model doubles replace external inference and local transports replace services.
These results do not establish hosted latency savings, a successful hosted Fleet chain,
deployment compatibility on Python 3.11, browser acceptance, or restart persistence.
Local runs use Python 3.13. Existing test skips/warnings were not suppressed or weakened.
