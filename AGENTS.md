# apx-agent Codex Guidance

This repository builds governed data-agent infrastructure for Databricks. The
Python package lives in `python/` (`src/apx_agent`), the TypeScript package
lives in `typescript/`, and the hub UI lives in `hub/`.

## Operating Principles

Apply these to every change:

- Ponytail: choose the smallest approach that works. Reuse existing primitives,
  helpers, declared options, and test patterns before adding new abstractions.
- Ctk: verify claim versus reality. After writing or generating anything, read
  it back and prove the behavior or artifact is real.
- Keep edits scoped to the requested behavior. Do not refactor unrelated files
  or weaken tests to get green.
- Stop and ask before deleting code outside the change scope, weakening or
  skipping a test, deploying, or taking production/external actions.

## Commands

Use the narrowest useful command while iterating, then run the full gate before
claiming a change works:

```bash
make check
cd python && uv run pytest
cd python && uv run pytest -k NAME
pre-commit run --all-files
```

`make check` is the read-after-write gate. It runs the full pytest suite,
including the `*_reality_ctk.py` tests, and then sanitizes the Python lockfile
registry metadata.

The lint suite is configured in `.pre-commit-config.yaml` and enforces local
style constraints such as no skipped tests, no empty-string default coercion, no
invented env defaults, no `object` annotations, and no tuple-return APIs.

## Review Style

When asked to review a branch or PR, use code-review stance:

- Lead with findings, ordered by severity.
- Include exact file and line references.
- Focus on bugs, regressions, security/governance risks, behavior drift,
  missing tests, and claim-vs-reality mismatches.
- Keep summary secondary and brief.
- If there are no findings, say that clearly and list remaining test gaps or
  residual risk.

Use this review prompt shape:

```text
Review this branch against origin/main. Use code-review stance: findings first,
severity ordered, exact file/line refs. Focus on bugs, regressions, missing
tests, security/governance risks, and claim-vs-reality drift. Apply apx-agent
Ponytail and Ctk. Do not suggest broad refactors.
```

## Issue Logging

Only log confirmed issues. Do not create speculative issues without evidence.

For each issue, capture:

- title
- severity
- affected files or commands
- evidence
- reproduction steps
- expected behavior
- smallest proposed fix
- suggested labels

Suggested labels:

- `bug`
- `ctk`
- `documentation`
- `security`
- `follow-up`
- `tests`
- `typescript`
- `python`

Use `gh issue create` after preparing an issue body, or paste the issue-ready
block into GitHub when direct GitHub writes are not intended.

## Domain Notes

- The project thesis is "declared, not wired": agent declarations should compile
  to Databricks runtime behavior without hidden hand-wiring.
- Runtime identity and governance matter. Be suspicious of changes that bypass
  Unity Catalog permissions, user identity propagation, approval pauses,
  tracing, or audit metadata.
- For docs, check whether future-tense claims have been overtaken by shipped
  behavior. `ctk.docs_direction` exists for LLM-assisted doc direction review,
  but its verdicts must be backed by exact quoted evidence.
