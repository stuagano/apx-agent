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
_TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".py",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".txt",
        ".html",
        ".css",
        ".js",
        ".ts",
    }
)
_PRIVATE_CUSTOMER_TERMS = ("curinos",)


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


def _assert_no_private_terms(
    paths: list[Path],
    *,
    terms: tuple[str, ...],
    root: Path,
) -> None:
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore").casefold()
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
