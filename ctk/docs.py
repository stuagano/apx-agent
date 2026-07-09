"""
Deterministic doc-staleness detection.

Mirrors ctk.lint.find_swallowed_exceptions: scan a set of docs and return a
list of Finding objects (empty == clean). Pure function of repo contents — no
wall-clock, no network — so it can back a `cheap` caps capability whose proof
is honest under fingerprint freshness.

Detectors: broken_ref, dead_link, orphan, superseded, assertion_failed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

_CODE_SPAN = re.compile(r"`([^`]+)`")
_PLACEHOLDER = re.compile(r"(path/to/|/\.\.\.|<[^>]+>|\bexample/|\bfoo/|\bbar/|\$\{)")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SPEC_SLUG = re.compile(r"(\d{4}-\d{2}-\d{2})-(.+?)(?:-design|-discovery)?\.md$")
_SUPERSEDE_PROSE = re.compile(
    r"(superseded|replaced|deprecated)\s+by\s+\[[^\]]*\]\(([^)]+)\)", re.IGNORECASE
)


@dataclass
class Finding:
    doc: str
    line: int | None
    kind: str  # broken_ref | dead_link | orphan | superseded | assertion_failed
    severity: str  # error | warn
    message: str
    evidence: str = ""

    def __str__(self) -> str:
        loc = f"{self.doc}:{self.line}" if self.line else self.doc
        ev = f"  ({self.evidence})" if self.evidence else ""
        return f"{loc}  [{self.kind}/{self.severity}]  {self.message}{ev}"


@dataclass
class DocsConfig:
    doc_roots: Sequence[str] = ("docs/", "README.md", "SKILL.md", "CLAUDE.md")
    entrypoints: Sequence[str] = ("README.md", "SKILL.md", "CLAUDE.md")
    ignore: Sequence[str] = ()  # regexes: path-like tokens to skip
    # Immutable archival trees (e.g. design-time specs) that must not be drift-checked
    # by ANY detector — paths are repo-relative prefixes.
    scan_exempt: Sequence[str] = ("docs/superpowers/",)
    orphan_exempt: Sequence[str] = ("docs/superpowers/",)
    known_top_dirs: Sequence[str] = ("caps/", "ctk/", "bin/", "tests/", "docs/", "examples/")
    tracked_ext: Sequence[str] = (
        ".py",
        ".md",
        ".sh",
        ".yaml",
        ".yml",
        ".txt",
        ".ini",
        ".toml",
        ".json",
    )
    severity_overrides: dict = field(default_factory=dict)  # kind -> severity
    direction: dict = field(default_factory=dict)  # consumed by docs_direction

    @classmethod
    def from_yaml(cls, path: str) -> DocsConfig:
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}
        known = {f_.name for f_ in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


def _severity(kind: str, config: DocsConfig, default: str) -> str:
    return config.severity_overrides.get(kind, default)


def _looks_like_repo_path(tok: str, config: DocsConfig) -> bool:
    tok = tok.strip()
    if not tok or tok.startswith(("http://", "https://", "#", "mailto:", "/")):
        return False
    if _PLACEHOLDER.search(tok):
        return False
    if any(re.search(p, tok) for p in config.ignore):
        return False
    if tok.startswith(tuple(config.known_top_dirs)):
        return True
    return "/" in tok and tok.endswith(tuple(config.tracked_ext))


def _exists(rel_path: str, repo_root: str) -> bool:
    rel_path = rel_path.split("#", 1)[0].strip()
    return bool(rel_path) and os.path.exists(os.path.join(repo_root, rel_path))


def _slugify_heading(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s+", "-", s)


def _anchors_in(path: str, repo_root: str) -> set[str]:
    out: set[str] = set()
    with open(os.path.join(repo_root, path), errors="replace") as f:
        for line in f:
            m = re.match(r"#{1,6}\s+(.*)", line)
            if m:
                out.add(_slugify_heading(m.group(1)))
    return out


def _is_relative_repo_target(target: str) -> bool:
    target = target.strip()
    return bool(target) and not target.startswith(("http://", "https://", "mailto:", "#", "/"))


def _detect_broken_refs(doc: str, text: str, repo_root: str, config: DocsConfig) -> list[Finding]:
    out: list[Finding] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for m in _CODE_SPAN.finditer(line):
            tok = m.group(1)
            if _looks_like_repo_path(tok, config) and not _exists(tok, repo_root):
                out.append(
                    Finding(
                        doc,
                        i,
                        "broken_ref",
                        _severity("broken_ref", config, SEVERITY_ERROR),
                        "code-span path does not exist",
                        tok,
                    )
                )
    return out


def _detect_dead_links(doc: str, text: str, repo_root: str, config: DocsConfig) -> list[Finding]:
    out: list[Finding] = []
    doc_dir = os.path.dirname(doc)
    for i, line in enumerate(text.splitlines(), start=1):
        for m in _MD_LINK.finditer(line):
            target = m.group(1).strip()
            if not _is_relative_repo_target(target):
                continue
            file_part, _, anchor = target.partition("#")
            rel = os.path.normpath(os.path.join(doc_dir, file_part)) if file_part else doc
            if not os.path.exists(os.path.join(repo_root, rel)):
                out.append(
                    Finding(
                        doc,
                        i,
                        "dead_link",
                        _severity("dead_link", config, SEVERITY_ERROR),
                        "link target does not exist",
                        target,
                    )
                )
            elif anchor:
                try:
                    anchors = _anchors_in(rel, repo_root)
                except OSError as e:
                    out.append(
                        Finding(
                            doc,
                            i,
                            "dead_link",
                            _severity("dead_link", config, SEVERITY_ERROR),
                            f"could not read link target to verify anchor: {e}",
                            target,
                        )
                    )
                    continue
                if _slugify_heading(anchor) not in anchors:
                    out.append(
                        Finding(
                            doc,
                            i,
                            "dead_link",
                            _severity("dead_link_anchor", config, SEVERITY_WARN),
                            "link anchor not found in target",
                            target,
                        )
                    )
    return out


def _outgoing_doc_links(doc: str, text: str, repo_root: str) -> list[str]:
    doc_dir = os.path.dirname(doc)
    out: list[str] = []
    for m in _MD_LINK.finditer(text):
        target = m.group(1).strip()
        if not _is_relative_repo_target(target):
            continue
        file_part = target.partition("#")[0]
        if not file_part.endswith(".md"):
            continue
        rel = os.path.normpath(os.path.join(doc_dir, file_part))
        if os.path.exists(os.path.join(repo_root, rel)):
            out.append(rel)
    return out


def _detect_orphans(
    docs: list[str], texts: dict[str, str], repo_root: str, config: DocsConfig
) -> list[Finding]:
    reachable: set[str] = set()
    frontier = [
        os.path.normpath(e)
        for e in config.entrypoints
        if os.path.exists(os.path.join(repo_root, e))
    ]
    reachable.update(frontier)
    while frontier:
        cur = frontier.pop()
        for nxt in _outgoing_doc_links(cur, texts.get(cur, ""), repo_root):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    out: list[Finding] = []
    exempt = tuple(config.orphan_exempt)
    entry = {os.path.normpath(e) for e in config.entrypoints}
    for doc in docs:
        if doc in reachable or doc in entry or doc.startswith(exempt):
            continue
        out.append(
            Finding(
                doc,
                None,
                "orphan",
                _severity("orphan", config, SEVERITY_WARN),
                "doc is not reachable from any entrypoint",
                doc,
            )
        )
    return out


def _front_matter(text: str) -> tuple[dict, int]:
    """Return (front_matter_dict, lines_consumed). Empty dict if none.

    Raises ValueError on malformed YAML so the caller can record an error
    Finding rather than swallowing it.
    """
    if not text.startswith("---\n"):
        return {}, 0
    end = text.find("\n---", 4)
    if end == -1:
        return {}, 0
    block = text[4:end]
    import yaml

    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML in front matter: {e}") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("front matter is not a mapping")
    return data, block.count("\n") + 2


def _detect_assertions(doc: str, text: str, repo_root: str, config: DocsConfig) -> list[Finding]:
    out: list[Finding] = []
    sev = _severity("assertion_failed", config, SEVERITY_ERROR)
    fm, _ = _front_matter(text)
    ctk_block = fm.get("ctk") or {}
    if not isinstance(ctk_block, dict):
        return out
    for p in ctk_block.get("requires_paths", []) or []:
        if not os.path.exists(os.path.join(repo_root, str(p))):
            out.append(
                Finding(
                    doc, 1, "assertion_failed", sev, "requires_paths target does not exist", str(p)
                )
            )
    for entry in ctk_block.get("requires_grep", []) or []:
        if not isinstance(entry, dict):
            out.append(
                Finding(
                    doc,
                    1,
                    "assertion_failed",
                    sev,
                    "requires_grep entry is not a mapping",
                    str(entry),
                )
            )
            continue
        f_path = str(entry.get("file", ""))
        pattern = str(entry.get("pattern", ""))
        abs_p = os.path.join(repo_root, f_path)
        if not os.path.exists(abs_p):
            out.append(
                Finding(
                    doc, 1, "assertion_failed", sev, "requires_grep file does not exist", f_path
                )
            )
            continue
        with open(abs_p, errors="replace") as fh:
            if not re.search(pattern, fh.read()):
                out.append(
                    Finding(
                        doc,
                        1,
                        "assertion_failed",
                        sev,
                        f"requires_grep pattern not found: {pattern}",
                        f_path,
                    )
                )
    return out


def _detect_superseded(
    docs: list[str], texts: dict[str, str], repo_root: str, config: DocsConfig
) -> list[Finding]:
    out: list[Finding] = []
    sev = _severity("superseded", config, SEVERITY_WARN)
    # newest date per spec slug
    latest: dict[str, str] = {}
    for doc in docs:
        m = _SPEC_SLUG.search(os.path.basename(doc))
        if m:
            date, slug = m.group(1), m.group(2)
            if slug not in latest or date > latest[slug]:
                latest[slug] = date
    for doc, text in texts.items():
        try:
            fm, _ = _front_matter(text)
        except ValueError:
            fm = {}  # malformed front matter is already reported by _detect_assertions
        if "superseded_by" in fm:
            out.append(
                Finding(
                    doc,
                    1,
                    "superseded",
                    sev,
                    "front-matter declares superseded_by",
                    str(fm["superseded_by"]),
                )
            )
            continue
        pm = _SUPERSEDE_PROSE.search(text)
        if pm:
            doc_dir = os.path.dirname(doc)
            tgt = os.path.normpath(os.path.join(doc_dir, pm.group(2).partition("#")[0]))
            if os.path.exists(os.path.join(repo_root, tgt)):
                out.append(
                    Finding(
                        doc,
                        None,
                        "superseded",
                        sev,
                        "prose says superseded/replaced/deprecated by",
                        pm.group(2),
                    )
                )
                continue
        m = _SPEC_SLUG.search(os.path.basename(doc))
        if m and m.group(1) < latest.get(m.group(2), m.group(1)):
            out.append(
                Finding(
                    doc,
                    None,
                    "superseded",
                    sev,
                    "a newer doc shares this spec slug",
                    f"newer: {latest[m.group(2)]}",
                )
            )
    return out


def _iter_docs(doc_roots: Sequence[str], repo_root: str) -> list[str]:
    """Return repo-relative paths of all .md docs under the given roots."""
    out: list[str] = []
    for root in doc_roots:
        abs_root = os.path.join(repo_root, root)
        if os.path.isfile(abs_root):
            if abs_root.endswith(".md"):
                out.append(os.path.relpath(abs_root, repo_root))
        elif os.path.isdir(abs_root):
            for dirpath, dirs, files in os.walk(abs_root):
                dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv"}]
                for fn in files:
                    if fn.endswith(".md"):
                        p = os.path.join(dirpath, fn)
                        out.append(os.path.relpath(p, repo_root))
    return sorted(set(out))


def find_stale_docs(
    doc_roots: Sequence[str] = ("docs/", "README.md", "SKILL.md", "CLAUDE.md"),
    repo_root: str = ".",
    config: DocsConfig | None = None,
) -> list[Finding]:
    config = config or DocsConfig(doc_roots=doc_roots)
    findings: list[Finding] = []
    docs = _iter_docs(doc_roots, repo_root)
    # Exclude immutable archival trees from all detectors.
    if config.scan_exempt:
        exempt = tuple(config.scan_exempt)
        docs = [d for d in docs if not d.startswith(exempt)]
    texts: dict[str, str] = {}
    for doc in docs:
        try:
            with open(os.path.join(repo_root, doc), errors="strict") as f:
                texts[doc] = f.read()
        except (OSError, UnicodeDecodeError) as e:
            findings.append(
                Finding(doc, None, "broken_ref", SEVERITY_ERROR, f"could not read doc: {e}", doc)
            )
    for doc, text in texts.items():
        findings.extend(_detect_broken_refs(doc, text, repo_root, config))
        findings.extend(_detect_dead_links(doc, text, repo_root, config))
        try:
            findings.extend(_detect_assertions(doc, text, repo_root, config))
        except ValueError as e:
            findings.append(
                Finding(
                    doc, 1, "assertion_failed", SEVERITY_ERROR, f"malformed front matter: {e}", doc
                )
            )
    findings.extend(_detect_orphans(list(texts), texts, repo_root, config))
    findings.extend(_detect_superseded(list(texts), texts, repo_root, config))
    return findings


def format_findings(findings: Sequence[Finding]) -> str:
    if not findings:
        return "no findings"
    return "\n".join("  " + str(f) for f in findings)
