"""The pull-request bead check, the bd pin, and hive push retry."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BEADS_PY = ROOT / "scripts" / "beads" / "beads.py"
PIN = (ROOT / "scripts" / "beads" / "BD_VERSION").read_text(encoding="utf-8").strip()

_spec = importlib.util.spec_from_file_location("apx_beads", BEADS_PY)
assert _spec is not None and _spec.loader is not None
beads = importlib.util.module_from_spec(_spec)
sys.modules["apx_beads"] = beads
_spec.loader.exec_module(beads)


def test_pin_is_the_only_copy() -> None:
    assert PIN == "1.4.0-rc.4"
    banned = [
        ROOT / "scripts" / "beads" / "setup_bd.sh",
        ROOT / "scripts" / "beads" / "beads.py",
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / ".github" / "workflows" / "beads-issue.yml",
        ROOT / ".github" / "workflows" / "beads-pr.yml",
        ROOT / ".github" / "workflows" / "beads-merge.yml",
        ROOT / ".github" / "workflows" / "beads-sync.yml",
    ]
    for path in banned:
        assert PIN not in path.read_text(encoding="utf-8"), path.name
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "scripts/beads/BD_VERSION" in agents
    assert "bd update <id> --claim" in agents
    assert "Never delete a closed bead" in agents


def test_repo_name_is_not_a_bead() -> None:
    result = beads.evaluate_pr("fix apx-agent", "see apx-agent", "cursor/apx-agent-docs", [], "octocat")
    assert result.ok is False
    assert result.bead_ids == []


def test_branch_title_and_child_ids_count() -> None:
    by_branch = beads.evaluate_pr("fix", "", "wt/bead/issue/apx-om5", [], "octocat")
    assert by_branch.ok is True
    assert by_branch.bead_ids == ["apx-om5"]
    by_body = beads.evaluate_pr("fix", "closes apx-ije.2.", "cursor/work-3518", [], "octocat")
    assert by_body.bead_ids == ["apx-ije.2"]
    by_title = beads.evaluate_pr("apx-merge-slot", "", "cursor/work-3518", [], "octocat")
    assert by_title.bead_ids == ["apx-merge-slot"]


def test_exempt_author_and_label() -> None:
    for author in ("dependabot[bot]", "github-actions[bot]"):
        result = beads.evaluate_pr("chore(deps): bump", "", "dependabot/uv", [], author)
        assert result.ok is True
        assert result.bead_ids == []
    labeled = beads.evaluate_pr("docs", "", "cursor/typo-3518", ["beads:exempt"], "octocat")
    assert labeled.ok is True


def test_close_still_sees_an_id_on_an_exempt_pr() -> None:
    ids = beads.referenced_bead_ids("chore apx-om5", "", "dependabot/uv")
    assert ids == ["apx-om5"]


def test_check_pr_cli_exit_codes(tmp_path: Path) -> None:
    body = tmp_path / "body.txt"
    body.write_text("no bead here\n", encoding="utf-8")
    missing = subprocess.run(
        [
            sys.executable,
            str(BEADS_PY),
            "check-pr",
            "--title",
            "fix",
            "--body-file",
            str(body),
            "--branch",
            "cursor/work-3518",
            "--author",
            "octocat",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 1
    present = subprocess.run(
        [
            sys.executable,
            str(BEADS_PY),
            "check-pr",
            "--title",
            "fix apx-om5",
            "--body-file",
            str(body),
            "--branch",
            "cursor/work-3518",
            "--author",
            "octocat",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert present.returncode == 0


def test_push_retries_then_succeeds() -> None:
    calls: list[list[str]] = []

    def execute(args: list[str]) -> beads.CmdResult:
        calls.append(args)
        if args[-1] == "push" and sum(1 for call in calls if call[-1] == "push") == 1:
            return beads.CmdResult(1, "", "rejected")
        return beads.CmdResult(0, "", "")

    pauses: list[int] = []
    beads.push_with_retry(execute, pauses.append)
    assert [call[-1] for call in calls] == ["push", "pull", "push"]
    assert pauses == [2]


def test_push_stops_after_the_attempt_budget() -> None:
    def execute(args: list[str]) -> beads.CmdResult:
        del args
        return beads.CmdResult(1, "", "rejected")

    with pytest.raises(SystemExit, match="bd dolt push failed"):
        beads.push_with_retry(execute, lambda _delay: None)
