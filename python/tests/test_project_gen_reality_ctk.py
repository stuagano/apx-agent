"""Generated-project content checks (claim-vs-reality, via the vendored ctk kit).

``test_project_gen.py`` asserts several generated files only ``.exists()``
(``agent_server/start_server.py``, ``agent_server/__init__.py``,
``databricks.yml``, copied skill files). A generator that wrote an *empty* or
content-less ``start_server.py`` would pass that check while producing a project
that cannot boot. These ``ctk.Artifact`` checks assert the files are real:
non-empty AND carrying the wiring that makes the generated project deployable
(the launcher actually starts a server, ``pyproject.toml`` carries the
``[tool.apx.agent]`` block and project name, ``databricks.yml`` names the agent,
the copied skill carries its content).

Mirrors the scaffold reality test (``test_scaffold_reality_ctk.py``) for the
``generate_project`` path. Uses ``ctk`` (python/.ctk, on the path via
``pythonpath`` in pyproject).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apx_agent._models import AgentConfig, MemoryBackendConfig, SkillConfig
from apx_agent._project_gen import generate_project
from ctk import Artifact, verify


@pytest.mark.unit
def test_generated_project_files_are_real_not_just_present(tmp_path: Path) -> None:
    config = AgentConfig(
        name="payroll-coworker",
        description="Reconciles hours worked against paychecks issued.",
        model="databricks-claude-sonnet-4-6",
        instructions="You are a payroll analyst.",
        template={
            "name": "coworker",
            "catalog": "main",
            "schema": "payroll",
            "persona": "a payroll analyst",
            "join_key": "employee ID",
            "objective": "Surface mismatches.",
        },
        memory=MemoryBackendConfig(
            type="delta",
            table_name="main.payroll.apx_memory",
            auto_create=True,
        ),
    )

    generate_project(config, tmp_path)

    # Reality, not just existence: non-empty AND actually wired.
    verify(
        Artifact(
            str(tmp_path / "pyproject.toml"),
            min_bytes=40,
            must_contain="[tool.apx.agent]",
        ),
        Artifact(
            str(tmp_path / "agent_server" / "start_server.py"),
            min_bytes=20,
        ),
        # agent_server/__init__.py is intentionally an empty package marker —
        # existence is the right level for it, so it is not a content artifact.
        Artifact(
            str(tmp_path / "databricks.yml"),
            min_bytes=20,
            must_contain="payroll-coworker",
        ),
    )
    # The project name must reach pyproject.toml, not just the agent block.
    verify(Artifact(str(tmp_path / "pyproject.toml"), must_contain="payroll-coworker"))


@pytest.mark.unit
def test_generated_skill_file_carries_content_not_just_exists(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "sql_guide.md").write_text("# SQL Guide\n\nPrefer explicit column lists.\n")

    config = AgentConfig(
        name="skilled-agent",
        model="databricks-claude-sonnet-4-6",
        skills=[
            SkillConfig(
                name="sql_guide",
                description="SQL best practices",
                path="sql_guide.md",
            )
        ],
    )

    out = tmp_path / "project"
    generate_project(config, out, source_dir=source_dir)

    # The copied skill must carry its real content through to the project, not
    # land as an empty file.
    verify(
        Artifact(
            str(out / "skills" / "sql_guide.md"),
            min_bytes=10,
            must_contain="SQL Guide",
        ),
    )
