# .ctk — vendored claude-test-kit (test-only helpers)

`ctk` is a thin layer over pytest for catching work that **claims success but
didn't do the thing** (exit 0 + empty output, swallowed exceptions, unvalidated
output). It lives in `.ctk/` — dot-prefixed and outside `src/` — so it is **never
part of the apx-agent runtime or wheel** (the wheel ships only `src/apx_agent`).

## Environment

Runs in apx-agent's own env. Invoke tests the normal way:

```bash
cd python && uv run pytest
```

`pythonpath = [".ctk"]` (in `python/pyproject.toml`) puts `ctk` on the path, so
tests run in the **same uv environment the app runs in** — no separate venv, no
dependency drift between "tests pass" and "localhost works".

## Using the helpers (always available)

```python
from ctk import run, expect, Artifact, verify

r = run(["python", "tool.py", "--out", "result.json"])  # strict: asserts exit code
r.ok()
expect(r.stdout).nonempty().matches(r"Processed \d+ rows")
verify(Artifact("result.json", min_bytes=2, is_json=True, json_keys=["rows"]))
```

No plugin needed for these — just import.

## Fixtures + error-log guard (opt-in)

The `workspace` / `run_started_at` fixtures and the autouse `fail_on_error_log`
guard (fails a test whose code logged ERROR/CRITICAL) are **opt-in**, so they do
not touch the existing ~200 apx-agent tests. Enable for one test tree:

```python
# python/tests/<area>/conftest.py
from ctk.pytest_plugin import workspace, run_started_at, fail_on_error_log  # noqa: F401
```

The guard then applies to that directory and below. Opt a single test out with
`@pytest.mark.allow_error_logs` (marker registered in `python/pyproject.toml`).

Whole-run alternative: `uv run pytest -p ctk.pytest_plugin ...`.

## Updating

Source of truth is the standalone `claude-test-kit` repo. To refresh, re-copy the
`ctk/` package modules here (the kit's `conftest.py` is vendored as
`ctk/pytest_plugin.py` to make it opt-in).
