# apx-agent — working in this repo

apx-agent is infrastructure for building governed data agents on Databricks: you
declare an agent (a Python object or a `[tool.apx.agent]` block) and apx-agent
compiles it to a Databricks runtime, grounds it in Unity Catalog, runs its tools
under UC governance, and makes it observable. Thesis: **declared, not wired.**

The Python package lives in `python/` (`src/apx_agent`). The TypeScript surface
is in `typescript/`; the deployable hub UI is in `hub/`.

## Two disciplines (apply to every change)

**Ponytail — don't over-engineer.** Pick the smallest approach that works.
Reuse an existing primitive, helper, or declared option before adding code.
Remove any abstraction, flag, or branch the tests don't exercise. Several of
these smells are enforced by the lint suite (no `.get(k, "")`, no `x or ""`, no
invented env defaults, no `object` annotations, no `tuple[...]` returns, no
skipped tests).

**Ctk — claim-vs-reality, read after write.** A change is not done because a
command exited 0. After producing or modifying anything, read it back and prove
it is real before you say it works:

- run the gate (`make check`) — it includes the `*_reality_ctk.py` tests;
- for an artifact a change writes, assert it is non-empty AND carries the wiring
  that makes it work, via `ctk.verify(Artifact(path, min_bytes=…, must_contain=…))`
  — not just `.exists()`;
- never report a green exit code as success without the read-back.

The `ctk` kit (`python/.ctk`, on the path via `pythonpath = [".ctk"]`) provides
`run`, `expect`, `Artifact`, `verify`, and `find_swallowed_exceptions`. It is
test-only and never ships in the wheel.

## The development loop

Looping is the connective tissue between the two disciplines and continuous
improvement. Each pass:

1. **Choose** the smallest in-scope change (Ponytail).
2. **Act** — make one bounded, reversible change.
3. **Verify** — `make check` and `pre-commit run --all-files`; read artifacts
   back with `ctk` (Ctk). Keep the change only if both stay green.
4. **Record** what changed and what's left; **repeat or stop** when the gate is
   green and there's no further measurable improvement.

Stop and ask before deleting code outside the change's scope, weakening or
skipping a test to get green, or any deploy / production / external action.

Worked loop scaffolds: [`docs/loops/README.md`](docs/loops/README.md) and the
loop-library skill at `.claude/skills/loop-library`.

## Commands

```bash
make check                          # verify gate — runs the full pytest suite
cd python && uv run pytest          # same suite directly (2600+ tests)
cd python && uv run pytest -k NAME  # a subset while iterating
pre-commit run --all-files          # lint suite (.pre-commit-config.yaml)
```

`make check` is the read-after-write gate; run it before claiming a change works.
