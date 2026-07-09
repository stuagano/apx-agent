from __future__ import annotations

import glob
import hashlib
import os
from pathlib import Path

from .manifest import Capability

# Above this many resolved files, skip recording the per-file map: a broad glob
# (e.g. "src/**") makes "which file changed" noise, and itemizing every hash
# would bloat the committed ledger. Such caps fall back to a plain code-stale.
FILE_MAP_LIMIT = 25


def _is_artifact(p: Path) -> bool:
    # Derived build artifacts must never be hashed — they regenerate on import
    # and would make a capability perpetually "stale" with no source change.
    return "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo"}


def _collect_files(capability: Capability, root: Path) -> list[Path]:
    files: list[Path] = []
    # The check file itself (pytest node "path::test" -> "path"). Shell checks
    # have no single source file, so only their deps are hashed.
    if capability.check_kind == "pytest":
        files.append(root / capability.check_target.split("::", 1)[0])
    for pattern in capability.deps:
        for match in glob.glob(str(root / pattern), recursive=True):
            p = Path(match)
            if p.is_file() and not _is_artifact(p):
                files.append(p)
    return files


def _resolve_rels(capability: Capability, root: Path) -> list[str]:
    """The deduped, sorted root-relative paths that make up a capability's
    fingerprint surface (check file + dep matches, build artifacts excluded).

    Keys are posix-normalized and stay relative to root even for a dep that
    escapes it (a ``../`` path rather than an absolute host path), so the ledger
    they land in stays portable across machines.
    """
    root = root.resolve()
    seen: set[str] = set()
    for f in _collect_files(capability, root):
        f = f if f.is_absolute() else (root / f)
        abs_f = f.resolve()
        try:
            rel = abs_f.relative_to(root).as_posix()
        except ValueError:
            rel = Path(os.path.relpath(abs_f, root)).as_posix()
        seen.add(rel)
    return sorted(seen)


def _hash_file(p: Path) -> str:
    """A `sha256:` digest of a file's bytes, or of a `<missing>` marker when the
    file is absent (so deletion changes the hash)."""
    h = hashlib.sha256()
    h.update(p.read_bytes() if p.is_file() else b"<missing>")
    return "sha256:" + h.hexdigest()


def fingerprint(capability: Capability, root: str | Path) -> str:
    """Hash the check file plus every file matched by deps globs.

    Deterministic: files are sorted by their path relative to root. A missing
    file hashes as a literal "<missing>" marker so deletion changes the result.
    """
    root = Path(root)
    h = hashlib.sha256()
    for rel in _resolve_rels(capability, root):
        p = root / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.is_file() else b"<missing>")
    return "sha256:" + h.hexdigest()


def file_fingerprints(capability: Capability, root: str | Path) -> dict:
    """Per-file hashes ({rel: 'sha256:..'}) over the same surface fingerprint()
    covers. Recorded at proof time so a later code-stale can report *which* file
    changed, not merely that one did."""
    root = Path(root)
    return {rel: _hash_file(root / rel) for rel in _resolve_rels(capability, root)}


def changed_deps(capability: Capability, recorded: dict | None, root: str | Path) -> list[str]:
    """Files that differ from the recorded per-file proof. `recorded` is the
    stored {rel: hash} map (None on older proofs or broad globs). Returns the
    sorted paths added/removed/modified since the proof; empty when it can't be
    determined."""
    if not recorded:
        return []
    current = file_fingerprints(capability, root)
    return sorted(r for r in set(current) | set(recorded) if current.get(r) != recorded.get(r))
