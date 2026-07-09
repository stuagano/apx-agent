from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from .backup import backup_file
from .manifest import ManifestError, load_manifest

HEADER = (
    "# Capabilities this project promises — managed with `caps`.\n"
    "# Prove with: python -m caps verify\n"
    "capabilities:\n"
)


class ManifestEditError(Exception):
    """Raised when a capability cannot be added safely; the manifest on disk is
    left unchanged."""


def _entry_block(entry: dict) -> str:
    """YAML for one capability, indented two spaces as a list item under
    `capabilities:`. safe_dump handles escaping of arbitrary scalar values."""
    dumped = yaml.safe_dump([entry], sort_keys=False, default_flow_style=False).rstrip("\n")
    return "\n".join(("  " + line) if line else line for line in dumped.split("\n"))


def _validate_candidate(candidate_text: str, new_id: str) -> None:
    """Parse the candidate manifest in a temp file. Accept only if it parses AND
    contains new_id. Raises ManifestEditError otherwise (disk never touched)."""
    fd, tmp_name = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(candidate_text)
        try:
            caps = load_manifest(tmp)
        except ManifestError as e:
            raise ManifestEditError(
                f"appended entry produced an invalid manifest ({e}); "
                f"is `capabilities:` block-style? manifest unchanged"
            ) from e
    finally:
        tmp.unlink(missing_ok=True)
    if new_id not in [c.id for c in caps]:
        raise ManifestEditError(
            f"appended entry for {new_id!r} did not load as a list item "
            f"(check `capabilities:` is a block-style list); manifest unchanged"
        )


def _scaffold_stub(root: Path, check: str, cap_id: str) -> None:
    rel, _, test_name = check.partition("::")
    test_name = test_name or "test_capability"
    target = root / rel
    if target.exists():
        return  # never clobber a real check
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Scaffolded by `caps add` — replace with a real "
        "write -> readback -> teardown check.\n"
        f"def {test_name}():\n"
        f'    raise NotImplementedError("implement the capability check for {cap_id}")\n'
    )


def add_capability(
    manifest_path: str | Path,
    *,
    id: str,
    description: str,
    given: str,
    when: str,
    then: str,
    tier: str,
    deps: list[str],
    check: str | None = None,
    shell: str | None = None,
) -> None:
    manifest_path = Path(manifest_path)
    if (check is None) == (shell is None):
        raise ManifestEditError("provide exactly one of check= or shell=")

    if manifest_path.exists():
        existing = load_manifest(manifest_path)
        if id in [c.id for c in existing]:
            raise ManifestEditError(f"capability id {id!r} already exists")
        existing_text = manifest_path.read_text()
        if not existing_text.endswith("\n"):
            existing_text += "\n"
    else:
        existing_text = HEADER

    entry: dict = {
        "id": id,
        "description": description,
        "given": given,
        "when": when,
        "then": then,
        "tier": tier,
        "deps": list(deps),
    }
    entry["check"] = check if check is not None else {"shell": shell}

    candidate_text = existing_text + _entry_block(entry) + "\n"
    _validate_candidate(candidate_text, id)  # raises if bad; disk still untouched

    if manifest_path.exists():
        backup_file(manifest_path)
    manifest_path.write_text(candidate_text)

    if check is not None:
        _scaffold_stub(manifest_path.parent, check, id)
