# Authoritative output completion: local validation

## Scope and state

- Owner: `authoritative-tool-completion`, branch `stuartg-dev/authoritative-tool-completion`.
- Intentional exact-deployed base: `0541b1c3d57a5a4d345056aab2b85e9f78732bd7`.
- Implementation and validation were initially authorized locally. The user subsequently
  authorized push, PR creation, and merge, then explicitly approved promoting the full
  prerequisite stack and resolving its conflicts with current main. Package publication,
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

The original completion fix and its report/Curinos patch were committed as `81dbe4a2`
and pushed. The handoff receipt records the original local-only authorization;
subsequent user instructions supersede that limit for push, PR creation, and merge.

## Main integration and promotion

PR #993 includes the twelve native-runtime prerequisites absent from main, the
completion fix, and the merge of `e7548e92` into this branch. The exact deployed
base remains in branch history; the implementation was not rebased onto main.

- Retained main's Python settings resolution and extended its binding types to
  include `NativeBinding`.
- Retained exact host-returned checkpoint verification rather than searching
  recently active sessions for a matching answer.
- Selected native workflow chat for declared input/output schemas, including
  nested leaves and Python-only declarations. Ordinary agents retain main's
  AgentKit chat. Typed hosts keep Apps opt-in for developer chat and exclude
  the full developer router's legacy execution actions. Both preserve project
  root-page ownership.
- Made the existing authoritative JSON-text requirement explicit before
  guardrails, and clarified non-null invariants for type checking.
- Independent integration review found the competing chat mounts and the native
  binding annotation mismatch. Both were fixed and independently re-reviewed;
  no remaining findings were reported in the bounded source review.

The first combined full gate found nine fixture/expectation failures: eight
deployment tests expected the old smoke argument set, and one typed-smoke fixture
lacked the project file read by main's new App-name resolution. Their assertions
now cover the combined contract, retaining the pre-provisioning rejection check.
An earlier focused run also caught developer-chat fixture setup occurring after
host startup; the environment is now set before the fixture starts.

The full Python type check initially found six errors in prerequisite code.
After explicit validation and type narrowing, it reports **0 errors, 4 warnings**.
The warnings concern two single-use TypeVars and two optional PostgreSQL checkpoint
imports. [Log](.build/integration-pyright-final.log).
Final native contract tests: **160 passed**; settings/chat tests: **33 passed**.
Changed-file lint passes. A broader all-file lint scan reports pre-existing
violations in unrelated example files; no lint rule was disabled.
The corrected integration against `e7548e92` passed `make check`: **5,537 passed,
13 skipped, 222 warnings**, with both public-PyPI lock checks passing.
[Log](.build/integration-check-final.log).

The initial merged Curinos run returned **944 passed, 4 failed**. All four
failures were staged native launcher checks: main's new settings resolution
correctly rejects the project's explicit Sonnet TOML model conflicting with its
Python leaf's Opus model. [curinos-native-settings.patch](curinos-native-settings.patch)
removes the redundant TOML declaration, leaving Python settings and `APX_MODEL`
as the model source. It was applied only to the copied source and independently
reviewed; the original checkout remains untouched. `git apply --check` passes
against the original checkout. All **12 startup tests** then passed, and the
native workflow suite passed **22 tests**. Adopting this merged APX stack in
Curinos requires both supplied compatibility patches.

## Proof limits

Local model doubles replace external inference and local transports replace services.
These results do not establish hosted latency savings, a successful hosted Fleet chain,
deployment compatibility on Python 3.11, browser acceptance, or restart persistence.
Local runs use Python 3.13. Existing test skips/warnings were not suppressed or weakened.
