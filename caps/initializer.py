from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
FRAMEWORK_DIRS = ("ctk", "caps")
WRAPPER_RELS = (  # drop-in hook wrappers, merged into bin/ (never owning the dir)
    Path("bin") / "caps-stop-gate.sh",
    Path("bin") / "caps-ponytail.sh",
)
_CONFTEST_WARNING = (
    "kept your existing conftest.py; until you add the kit's `workspace` fixture "
    "and the autouse `fail_on_error_log` guard to it, the error-log guard is OFF "
    "and any vendored check using the `workspace` fixture will error. See the "
    "kit's conftest.py for the two fixtures to copy in."
)


@dataclass
class StepResult:
    """One thing `init` did (or chose not to do), for the CLI to print.

    action is one of: created | skipped | overwritten | warned | installed
                      | instructed
    """

    action: str
    target: str
    detail: str = ""


def kit_root() -> Path:
    """The kit being vendored: the dir containing the live caps/ package."""
    return Path(__file__).resolve().parent.parent


def _vendor_one(src: Path, dst: Path, force: bool) -> StepResult:
    name = dst.name
    if dst.exists():
        if not force:
            return StepResult("skipped", str(dst), f"{name}/ already present")
        shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=_IGNORE)
        return StepResult("overwritten", str(dst), f"re-vendored {name}/ (--force)")
    shutil.copytree(src, dst, ignore=_IGNORE)
    return StepResult("created", str(dst), f"vendored {name}/")


def _vendor_wrapper(kit: Path, target: Path, force: bool, rel: Path) -> StepResult:
    """Copy one hook wrapper *file* into the target's bin/, merging into an
    existing bin/ rather than owning the directory — so a user's own bin/ scripts
    are never deleted (unlike the ctk/caps packages, bin/ is a generic dir name)."""
    src = kit / rel
    dst = target / rel
    name = rel.name
    if not src.is_file():
        return StepResult("skipped", str(dst), f"kit has no {name} to vendor")
    existed = dst.exists()
    if existed and not force:
        return StepResult("skipped", str(dst), f"bin/{name} already present")
    dst.parent.mkdir(parents=True, exist_ok=True)  # merge into existing bin/, never rmtree
    shutil.copy2(src, dst)
    if existed:
        return StepResult("overwritten", str(dst), f"re-vendored bin/{name} (--force)")
    return StepResult("created", str(dst), f"vendored bin/{name}")


def ensure_conftest(target: str | Path, kit: str | Path) -> StepResult:
    target, kit = Path(target), Path(kit)
    dst = target / "conftest.py"
    if dst.exists():
        return StepResult("warned", str(dst), _CONFTEST_WARNING)
    shutil.copy2(kit / "conftest.py", dst)
    return StepResult("created", str(dst), "copied conftest.py (workspace + error-log guard)")


_PYTEST_INI = """\
[pytest]
# Written by `caps init`. Lets vendored `ctk`/`caps` import without installing,
# and registers the markers their checks use.
pythonpath = .
addopts = -ra --strict-markers
markers =
    unit: fast, isolated tests with no real I/O (mock the boundaries)
    integration: tests that hit real dependencies (DB, HTTP, subprocess)
    slow: long-running tests, excluded from the quick loop
    allow_error_logs: permit ERROR/CRITICAL logs without failing the test
"""


def _has_pytest_config(target: Path) -> bool:
    if (target / "pytest.ini").is_file():
        return True
    pp = target / "pyproject.toml"
    if pp.is_file() and "[tool.pytest.ini_options]" in pp.read_text():
        return True
    sc = target / "setup.cfg"
    if sc.is_file() and "[tool:pytest]" in sc.read_text():
        return True
    tox = target / "tox.ini"
    return tox.is_file() and "[pytest]" in tox.read_text()


def ensure_pytest_config(target: str | Path) -> StepResult:
    target = Path(target)
    if _has_pytest_config(target):
        return StepResult(
            "skipped",
            str(target),
            "existing pytest config found; ensure it sets `pythonpath = .` and the "
            "unit/integration/slow/allow_error_logs markers (see the kit's pytest.ini)",
        )
    dst = target / "pytest.ini"
    dst.write_text(_PYTEST_INI)
    return StepResult("created", str(dst), "wrote minimal pytest.ini")


_STARTER_MANIFEST = """\
# Capabilities THIS project promises — managed with `caps`.
# Prove with:   python -m caps verify
# Check state:  python -m caps status
# Add one with: python -m caps add --id <id> --tier <cheap|live> \\
#                 --description "..." --given "..." --when "..." --then "..." \\
#                 --deps <glob> --check checks/test_<id>.py::test_<id>
#
# Example entry (run `caps add ...` to append one — don't hand-edit):
#   - id: writes-to-db
#     description: rows written by the ingest job read back with matching ids
#     given: a reachable database
#     when: the ingest job runs
#     then: the written rows are readable back
#     tier: live
#     deps: [ingest.py]
#     check: checks/test_db_write.py::test_write_readback
capabilities:
"""


def ensure_starter_manifest(target: str | Path) -> list[StepResult]:
    target = Path(target)
    results: list[StepResult] = []

    manifest = target / "capabilities.yaml"
    if manifest.exists():
        results.append(StepResult("skipped", str(manifest), "manifest already present"))
    else:
        manifest.write_text(_STARTER_MANIFEST)
        results.append(StepResult("created", str(manifest), "wrote starter capabilities.yaml"))

    checks = target / "checks"
    keep = checks / ".gitkeep"
    if keep.exists():
        results.append(StepResult("skipped", str(checks), "checks/ already present"))
    else:
        checks.mkdir(parents=True, exist_ok=True)
        keep.write_text("")
        results.append(StepResult("created", str(checks), "created checks/"))
    return results


_GITIGNORE_ENTRIES = (".venv/", "__pycache__/", ".pytest_cache/", "*.bak.*")


def ensure_gitignore(target: str | Path) -> StepResult:
    target = Path(target)
    gi = target / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [e for e in _GITIGNORE_ENTRIES if e not in present]
    if not missing:
        return StepResult("skipped", str(gi), ".gitignore already covers caps artifacts")

    block = "" if existing == "" else (existing if existing.endswith("\n") else existing + "\n")
    block += "\n# caps / python artifacts (added by caps init)\n" + "\n".join(missing) + "\n"
    gi.write_text(block)
    return StepResult(
        "created",
        str(gi),
        f"added {len(missing)} .gitignore entr{'y' if len(missing) == 1 else 'ies'}",
    )


def vendor_framework(target: str | Path, kit: str | Path, force: bool) -> list[StepResult]:
    target, kit = Path(target), Path(kit)
    if force and target.resolve() == kit.resolve():
        raise ValueError("init --force target is the kit itself; refusing to overwrite the source")
    results: list[StepResult] = []
    for name in FRAMEWORK_DIRS:
        src = kit / name
        if not src.is_dir():
            continue  # nothing to vendor for a kit missing this dir
        results.append(_vendor_one(src, target / name, force))
    for rel in WRAPPER_RELS:
        results.append(_vendor_wrapper(kit, target, force, rel))
    return results


def _pip_install(pkg: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", pkg], check=True)


def maybe_install_pyyaml(install_deps: bool) -> StepResult:
    line = f"{sys.executable} -m pip install PyYAML"
    if install_deps:
        _pip_install("PyYAML")
        return StepResult("installed", "PyYAML", "pip-installed PyYAML")
    return StepResult("instructed", "PyYAML", f"caps needs PyYAML — run: {line}")


def init_project(
    target: str | Path,
    *,
    kit: str | Path | None = None,
    force: bool = False,
    install_deps: bool = False,
) -> list[StepResult]:
    target = Path(target)
    kit = Path(kit) if kit is not None else kit_root()
    target.mkdir(parents=True, exist_ok=True)

    results: list[StepResult] = []
    results += vendor_framework(target, kit, force)
    results.append(ensure_conftest(target, kit))
    results.append(ensure_pytest_config(target))
    results += ensure_starter_manifest(target)
    results.append(ensure_gitignore(target))
    results.append(maybe_install_pyyaml(install_deps))
    return results
