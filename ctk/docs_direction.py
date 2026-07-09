"""
LLM-first doc *direction* review.

Component 1 (ctk.docs) catches mechanical drift deterministically. This module
makes the judgment a regex can't: has the project's content and direction moved
PAST this doc, regardless of age? It shells out to the `claude` CLI, then keeps
the verdict honest — an `overtaken` verdict is only trusted if the exact lines
it quotes really appear in the named files (see ctk.docs_direction.verify_*).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

VALID_VERDICTS = ("current", "overtaken", "uncertain")


class ClaudeUnavailable(Exception):
    """Raised when the `claude` CLI is not available to run the review."""


@dataclass
class DirectionVerdict:
    doc: str
    verdict: str  # current | overtaken | uncertain
    rationale: str
    doc_evidence: list[str] = field(default_factory=list)
    source_evidence: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.doc}  [{self.verdict}]  {self.rationale}"


def _authoritative_context(repo_root: str) -> str:
    parts: list[str] = []
    for name in ("README.md", "SKILL.md", "CLAUDE.md", "capabilities.yaml"):
        p = os.path.join(repo_root, name)
        if os.path.exists(p):
            with open(p, errors="replace") as f:
                parts.append(f"### {name}\n{f.read()}")
    return "\n\n".join(parts)


def _build_prompt(doc: str, doc_text: str, context: str) -> str:
    return (
        "You are auditing whether a project doc still matches the project's "
        "current direction. Consider supersession, work described as future "
        "that is now shipped, and decisions the current sources contradict. "
        "Do NOT judge on age alone.\n\n"
        "Reply with ONLY a JSON object: "
        '{"verdict": "current|overtaken|uncertain", "rationale": "...", '
        '"doc_evidence": ["exact quoted line from the doc"], '
        '"source_evidence": ["exact quoted line from a current source"]}. '
        "For 'overtaken', doc_evidence and source_evidence MUST be exact "
        "substrings copied verbatim from the texts below.\n\n"
        f"=== DOC UNDER REVIEW: {doc} ===\n{doc_text}\n\n"
        f"=== CURRENT AUTHORITATIVE SOURCES ===\n{context}\n"
    )


def _claude_cli_runner(prompt: str) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise ClaudeUnavailable("claude CLI not on PATH")
    try:
        proc = subprocess.run([exe, "-p", prompt], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as e:
        # A hung/too-slow CLI is "unavailable to review in band" → fail-open (skip),
        # same as a missing CLI. Don't let it error the check and block the turn.
        raise ClaudeUnavailable("claude CLI timed out after 180s") from e
    if proc.returncode != 0:
        raise ClaudeUnavailable(f"claude exited {proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout


def _quote_present(quote: str, haystacks: Sequence[str]) -> bool:
    q = " ".join(quote.split())
    return any(q and " ".join(h.split()).find(q) != -1 for h in haystacks)


def _verify_evidence(v: DirectionVerdict, doc_text: str, context: str) -> DirectionVerdict:
    if v.verdict != "overtaken":
        return v
    doc_ok = v.doc_evidence and all(_quote_present(q, [doc_text]) for q in v.doc_evidence)
    src_ok = v.source_evidence and all(_quote_present(q, [context]) for q in v.source_evidence)
    if doc_ok and src_ok:
        return v
    return DirectionVerdict(
        v.doc,
        "uncertain",
        f"overtaken claim discarded — evidence not verifiable ({v.rationale})",
        v.doc_evidence,
        v.source_evidence,
    )


def _parse_verdict(doc: str, raw: str) -> DirectionVerdict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return DirectionVerdict(doc, "uncertain", "no JSON in model output")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return DirectionVerdict(doc, "uncertain", f"unparseable verdict: {e}")
    verdict = data.get("verdict", "uncertain")
    if verdict not in VALID_VERDICTS:
        verdict = "uncertain"
    return DirectionVerdict(
        doc=doc,
        verdict=verdict,
        rationale=str(data.get("rationale", "")),
        doc_evidence=[str(x) for x in data.get("doc_evidence", []) or []],
        source_evidence=[str(x) for x in data.get("source_evidence", []) or []],
    )


def review_doc_direction(
    docs: Sequence[str],
    repo_root: str = ".",
    config=None,
    runner: Callable[[str], str] | None = None,
) -> list[DirectionVerdict]:
    runner = runner or _claude_cli_runner
    context = _authoritative_context(repo_root)
    verdicts: list[DirectionVerdict] = []
    for doc in docs:
        with open(os.path.join(repo_root, doc), errors="replace") as f:
            doc_text = f.read()
        raw = runner(_build_prompt(doc, doc_text, context))
        verdict = _parse_verdict(doc, raw)
        verdicts.append(_verify_evidence(verdict, doc_text, context))
    return verdicts


def format_verdicts(verdicts: Sequence[DirectionVerdict]) -> str:
    return "\n".join("  " + str(v) for v in verdicts) or "no verdicts"
