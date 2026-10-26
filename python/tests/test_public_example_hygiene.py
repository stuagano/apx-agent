from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_PRIVATE_CUSTOMER_TERMS = ("curinos",)


def _public_text_files() -> list[Path]:
    # ponytail: every tracked file — the repo is public, so any path can leak.
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    this_file = Path(__file__).resolve()
    return sorted(
        path
        for path in (_REPO / name for name in tracked)
        if path.resolve() != this_file
    )


def _assert_no_private_terms(
    paths: list[Path],
    *,
    terms: tuple[str, ...],
    root: Path,
) -> None:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").casefold()
        except (FileNotFoundError, NotADirectoryError):
            # The scan walks the live repo tree; a concurrent process (e.g. a
            # local bundle deploy regenerating an example's .build/) can unlink
            # a file between rglob and read. A vanished file carries no terms.
            continue
        for term in terms:
            if term.casefold() in text:
                raise AssertionError(
                    f"private customer term in {path.relative_to(root)}"
                )


def test_private_term_guard_reports_relative_path(tmp_path: Path) -> None:
    leaked = tmp_path / "leaked.md"
    leaked.write_text(
        f"sample {_PRIVATE_CUSTOMER_TERMS[0].upper()}", encoding="utf-8"
    )

    with pytest.raises(AssertionError, match="leaked.md") as caught:
        _assert_no_private_terms(
            [leaked],
            terms=_PRIVATE_CUSTOMER_TERMS,
            root=tmp_path,
        )

    assert _PRIVATE_CUSTOMER_TERMS[0] not in str(caught.value).lower()


def test_public_artifacts_do_not_contain_private_customer_terms() -> None:
    _assert_no_private_terms(
        _public_text_files(),
        terms=_PRIVATE_CUSTOMER_TERMS,
        root=_REPO,
    )
