# Public example hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Python test suite fail whenever a private customer name appears in a reusable APX artifact, so documentation, examples, generated source, and user-visible source strings stay safe to publish and copy.

**Architecture:** Reuse the existing lightweight source-scan test pattern. One test-only denylist walks a deliberately small group of public roots and known text suffixes. It excludes tests so the denylist itself can contain the protected term. No runtime scanner, CLI, new dependency, or separate CI workflow is needed because the ordinary Python test gate already discovers test files.

**Tech Stack:** Python 3.11+, `pathlib`, pytest, existing `uv --frozen` test gate.

**Spec:** [Declarative role pipelines](../declarative-role-pipelines.md)

## Global Constraints

- The literal protected terms live only in `python/tests/test_public_example_hygiene.py`. Do not repeat them in docs, plans, generated files, user-visible messages, PR titles, or examples.
- Scan exactly these public/reusable roots: `README.md`, `docs/`, `python/examples/`, and production `python/src/apx_agent/`.
- Exclude `python/tests/`, VCS metadata, generated build output, and unrelated local artifacts. The guard must not self-fail because it contains its own denylist.
- Include source templates and generator implementations under `python/src/apx_agent/`, not only finished examples.
- Use a narrow reviewed suffix allowlist. Decode with `errors="ignore"` so a non-text byte does not hide a matching public string.
- Failure output names only the protected term and repository-relative path. Do not echo file contents.
- Add a term only after confirming it is non-public customer material. Do not turn this into a broad or speculative vocabulary filter.
- Do not add a custom lint command, lockfile entry, dependency, or CI workflow. Standard pytest collection is the enforcement point.

## File map

- Create: `python/tests/test_public_example_hygiene.py` — one helper-unit proof and the repository-wide public-artifact scan.
- Modify only if a real scan failure identifies a confirmed private-name leak in a public root.

---

### Task 1: Build the test-only denylist and prove its failure mode

**Files:**
- Create: `python/tests/test_public_example_hygiene.py`

**Interfaces:**
- Consumes: `pathlib.Path`, pytest, and the repository layout.
- Produces: `_public_text_files()`, `_assert_no_private_terms()`, and `test_public_artifacts_do_not_contain_private_customer_terms()`.

- [ ] **Step 1: Start with a small reusable helper and a synthetic leak proof.**

Create the test file with this shape. Set the test-only tuple to the already confirmed protected term during implementation; this plan intentionally does not reproduce that private literal. Do not place the literal anywhere else.

~~~python
from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PUBLIC_ROOTS = (
    _REPO / "README.md",
    _REPO / "docs",
    _REPO / "python" / "examples",
    _REPO / "python" / "src" / "apx_agent",
)
_TEXT_SUFFIXES = frozenset({
    ".md", ".py", ".toml", ".yaml", ".yml", ".json",
    ".txt", ".html", ".css", ".js", ".ts",
})
# Set this tuple to the confirmed protected term only in this test file.
_PRIVATE_CUSTOMER_TERMS = (...,)
~~~

Implement:

~~~python
def _assert_no_private_terms(
    paths: list[Path],
    *,
    terms: tuple[str, ...],
    root: Path,
) -> None:
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for term in terms:
            assert term not in text, (
                f"private customer term {term!r} in {path.relative_to(root)}"
            )
~~~

Then prove the helper catches a leak without adding a real repository leak:

~~~python
def test_private_term_guard_reports_relative_path(tmp_path: Path) -> None:
    leaked = tmp_path / "leaked.md"
    leaked.write_text(f"sample {_PRIVATE_CUSTOMER_TERMS[0]}")

    with pytest.raises(AssertionError, match="leaked.md"):
        _assert_no_private_terms(
            [leaked],
            terms=_PRIVATE_CUSTOMER_TERMS,
            root=tmp_path,
        )
~~~

- [ ] **Step 2: Run the helper test.**

~~~bash
cd python && uv run --frozen pytest \
  tests/test_public_example_hygiene.py::test_private_term_guard_reports_relative_path -q
~~~

Expected: PASS. The test shows that a future leak reports a safe relative file path.

- [ ] **Step 3: Add public-file discovery and the actual repository assertion.**

Use a finite root list and only the suffixes above:

~~~python
def _public_text_files() -> list[Path]:
    paths: list[Path] = []
    for root in _PUBLIC_ROOTS:
        if root.is_file():
            paths.append(root)
        elif root.is_dir():
            paths.extend(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix in _TEXT_SUFFIXES
            )
    return sorted(paths)


def test_public_artifacts_do_not_contain_private_customer_terms() -> None:
    _assert_no_private_terms(
        _public_text_files(),
        terms=_PRIVATE_CUSTOMER_TERMS,
        root=_REPO,
    )
~~~

When first run finds a match, inspect it manually. Replace a confirmed leak with a neutral fictional example in the same focused change. If a potential match is genuinely public, reassess whether the denylist term belongs in this narrow guard; do not silently broaden exclusions.

- [ ] **Step 4: Run the full guard.**

~~~bash
cd python && uv run --frozen pytest tests/test_public_example_hygiene.py -q
~~~

Expected: PASS after any confirmed public leak is neutralized.

- [ ] **Step 5: Verify ordinary test discovery and commit the smallest scope.**

~~~bash
cd python && uv run --frozen pytest --collect-only -q \
  | rg 'test_public_artifacts_do_not_contain_private_customer_terms'
git diff --check
git status --short
~~~

Expected: the node is collected by normal pytest. Stage only this new test and any directly identified public-artifact fix:

~~~bash
git add python/tests/test_public_example_hygiene.py
git commit -m "test: guard public artifacts against private customer references"
~~~

If the scan identified a confirmed leak, add each changed public file by its explicit path between those commands. Do not stage a directory wholesale. Before staging any existing file, check it is not a symlink and inspect its diff.

---

### Task 2: Establish the durable enforcement boundary

**Files:**
- Verify only: `python/tests/test_public_example_hygiene.py` and existing project test configuration.

**Interfaces:**
- Consumes: existing pytest discovery and the repository `make check` gate.
- Produces: evidence that the guard is enforced by the normal code-quality path.

- [ ] **Step 1: Confirm test collection without a special workflow.**

Run:

~~~bash
cd python && uv run --frozen pytest --collect-only -q \
  | rg 'test_public_artifacts_do_not_contain_private_customer_terms'
~~~

Expected: one collected node. If it is absent, correct the test filename/function naming; do not create separate CI machinery.

- [ ] **Step 2: Run the normal quality gate after the companion graph implementation is ready.**

~~~bash
make check
~~~

Expected: the hygiene scan runs alongside the project suite. If the repository gate has an unrelated failure, report it independently and do not disable the scan.

- [ ] **Step 3: Report only the safety boundary, not the protected term.**

Report the exact test node, the public roots scanned, and whether a confirmed public leak was removed. Refer to the protected string only as “the private customer term.”
