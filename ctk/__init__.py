r"""
claude-test-kit (ctk)
=====================

A small, opinionated Python testing framework built to catch the specific way
agent/script work fails: it *claims* success but didn't actually do the thing.

Design goal: make silent failure impossible to ignore.

Four failure modes this kit is built to catch
----------------------------------------------
1. "Claude says done, isn't"  -> ctk.verify  (claim-vs-reality checks on real side effects)
2. No output validation       -> ctk.contracts (declarative checks on the actual output)
3. Swallowed exceptions       -> ctk.lint (AST scan) + no_error_logs fixture
4. Exit 0 but wrong output    -> ctk.runners (strict runner: exit code AND output asserted)

Quick start
-----------
    from ctk import run, expect, Artifact, verify

    # 1. Run a script strictly: exit code + output are both checked.
    r = run(["python", "my_tool.py", "--out", "result.json"])
    r.ok()                         # fails loudly if returncode != 0
    r.no_stderr_errors()           # fails if stderr looks like an error

    # 2. Validate the output actually matches what you expected.
    expect(r.stdout).nonempty().matches(r"Processed \d+ rows")

    # 3. Verify the agent's *claim* against reality.
    verify(
        Artifact("result.json", min_bytes=2, is_json=True, json_keys=["rows"]),
    )
"""

from .assertions import assert_eq, assert_file, assert_nonempty, must
from .contracts import ContractError, Expect, expect
from .docs import DocsConfig, Finding, find_stale_docs, format_findings
from .docs_direction import (
    ClaudeUnavailable,
    DirectionVerdict,
    format_verdicts,
    review_doc_direction,
)
from .lint import SwallowedExcept, find_swallowed_exceptions
from .runners import RunResult, run
from .verify import (
    Artifact,
    Checklist,
    VerificationError,
    claim_vs_reality,
    verify,
)

__all__ = [
    "run",
    "RunResult",
    "expect",
    "Expect",
    "ContractError",
    "must",
    "assert_nonempty",
    "assert_file",
    "assert_eq",
    "Artifact",
    "verify",
    "claim_vs_reality",
    "Checklist",
    "VerificationError",
    "find_swallowed_exceptions",
    "SwallowedExcept",
    "find_stale_docs",
    "Finding",
    "DocsConfig",
    "format_findings",
    "review_doc_direction",
    "DirectionVerdict",
    "ClaudeUnavailable",
    "format_verdicts",
]

__version__ = "0.1.0"
