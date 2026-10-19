"""Python tools use explicit imports and remain executable after native staging."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import click
import pytest

from apx_agent._agentbricks_deploy import stage_native_source


def _write_tools_package(root: Path) -> None:
    tools = root / "tools"
    (tools / "shared").mkdir(parents=True)
    (tools / "__init__.py").write_text('"""Explicitly imported project tools."""\n')
    (tools / "shared" / "__init__.py").write_text("")
    (tools / "shared" / "values.py").write_text("OFFSET = 40\n")
    (tools / "sales.py").write_text(
        "from .shared.values import OFFSET\n\n"
        "def sales(value: int) -> int:\n"
        '    """Return a locally computed sales value."""\n'
        "    return OFFSET + value\n"
    )
    # Ordinary Python imports must not eagerly discover unrelated tool modules.
    (tools / "unused.py").write_text("raise RuntimeError('must not be imported')\n")


@pytest.mark.parametrize("layout", ["package", "module"])
def test_staged_agent_imports_and_calls_tools_without_original_source(
    tmp_path: Path, layout: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    if layout == "package":
        _write_tools_package(project)
        import_line = "from tools.sales import sales\n"
    else:
        (project / "tools.py").write_text(
            "def sales(value: int) -> int:\n"
            '    """Return a locally computed sales value."""\n'
            "    return 40 + value\n"
        )
        import_line = "from tools import sales\n"
    (project / "agent.py").write_text(import_line + "tools = [sales]\n")
    authored = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*.py")
    }

    stage_native_source(project)
    # Relocate only the deployment output, then remove the entire source project.
    # The child cannot accidentally succeed by importing the authored originals.
    deployed = tmp_path / "deployment" / ".build"
    deployed.parent.mkdir()
    shutil.move(str(project / ".build"), deployed)
    shutil.rmtree(project)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", "\n".join([
            "import json, pathlib, sys",
            "sys.path.insert(0, str(pathlib.Path.cwd()))",
            "import agent",
            "import tools",
            "tool = agent.tools[0]",
            "print(json.dumps({",
            "    'value': tool(2),",
            "    'name': tool.__name__,",
            "    'description': tool.__doc__,",
            "    'files': [module.__file__ for name, module in sys.modules.items()",
            "              if name in ('agent', 'tools') or name.startswith('tools.')],",
            "    'modules': list(sys.modules),",
            "}))",
        ])],
        cwd=deployed, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    proof = json.loads(result.stdout)
    assert proof["value"] == 42
    assert proof["name"] == "sales"
    assert proof["description"] == "Return a locally computed sales value."
    assert "tools.unused" not in proof["modules"]
    expected = {"agent.py", "tools.py"} if layout == "module" else {
        "agent.py", "tools/__init__.py", "tools/sales.py",
        "tools/shared/__init__.py", "tools/shared/values.py",
    }
    assert {str(Path(path).relative_to(deployed)) for path in proof["files"]} == expected
    for relative, contents in authored.items():
        assert (deployed / relative).read_bytes() == contents


@pytest.mark.parametrize("existing_build", [False, True])
def test_native_staging_rejects_module_package_conflict_before_rebuild(
    tmp_path: Path, existing_build: bool,
) -> None:
    if existing_build:
        stage_native_source(tmp_path)
        (tmp_path / ".build" / "preserved.txt").write_text("previous deployment")
    _write_tools_package(tmp_path)
    (tmp_path / "tools.py").write_text("AUTHORED = True\n")

    with pytest.raises(click.ClickException, match=r"tools\.py.*tools/"):
        stage_native_source(tmp_path)

    assert (tmp_path / "tools.py").read_text() == "AUTHORED = True\n"
    assert (tmp_path / "tools" / "sales.py").is_file()
    if existing_build:
        assert (tmp_path / ".build" / "preserved.txt").read_text() == "previous deployment"
    else:
        assert not (tmp_path / ".build").exists()


@pytest.mark.parametrize("hazard", ["package-link", "nested-link", "broken-link"])
def test_tools_package_symlinks_are_rejected_before_rebuild(tmp_path: Path, hazard: str) -> None:
    stage_native_source(tmp_path)
    (tmp_path / ".build" / "preserved.txt").write_text("previous deployment")
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "authored.py").write_text("preserve me")
    if hazard == "package-link":
        (tmp_path / "tools").symlink_to(protected, target_is_directory=True)
    else:
        _write_tools_package(tmp_path)
        target = protected / ("missing.py" if hazard == "broken-link" else "authored.py")
        (tmp_path / "tools" / "shared" / "link.py").symlink_to(target)

    with pytest.raises(click.ClickException, match="symlinked"):
        stage_native_source(tmp_path)

    assert (protected / "authored.py").read_text() == "preserve me"
    assert (tmp_path / ".build" / "preserved.txt").read_text() == "previous deployment"


def test_tools_package_staging_preserves_unowned_build(tmp_path: Path) -> None:
    _write_tools_package(tmp_path)
    (tmp_path / ".build").mkdir()
    (tmp_path / ".build" / "authored.py").write_text("preserve me")

    with pytest.raises(click.ClickException, match="unowned"):
        stage_native_source(tmp_path)

    assert (tmp_path / ".build" / "authored.py").read_text() == "preserve me"


def test_tools_package_rebuild_removes_stale_modules(tmp_path: Path) -> None:
    _write_tools_package(tmp_path)
    stage_native_source(tmp_path)
    staged = tmp_path / ".build" / "tools"
    assert (staged / "unused.py").read_text() == "raise RuntimeError('must not be imported')\n"

    (tmp_path / "tools" / "unused.py").unlink()
    stage_native_source(tmp_path)

    assert not (staged / "unused.py").exists()
    assert (staged / "sales.py").read_bytes() == (tmp_path / "tools" / "sales.py").read_bytes()
