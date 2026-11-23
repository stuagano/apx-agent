"""The pull-request bead check, issue-state sync, the bd pin, and hive push retry."""

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


def test_plan_issue_sync_follows_github_state() -> None:
    assert beads.plan_issue_sync("open", None) == "create"
    assert beads.plan_issue_sync("OPEN", None) == "create"
    assert beads.plan_issue_sync("closed", None) == "skip"
    assert beads.plan_issue_sync("CLOSED", "open") == "close"
    assert beads.plan_issue_sync("closed", "in_progress") == "close"
    assert beads.plan_issue_sync("closed", "closed") == "unchanged"
    assert beads.plan_issue_sync("open", "closed") == "reopen"
    assert beads.plan_issue_sync("OPEN", "in_progress") == "unchanged"
    with pytest.raises(SystemExit, match="unsupported issue state"):
        beads.plan_issue_sync("reopened", "closed")


def test_issues_from_payload_normalizes_cli_state() -> None:
    rows = beads.issues_from_payload(
        [
            {
                "number": 3,
                "title": "closed upstream",
                "url": "https://github.com/stuagano/apx-agent/issues/3",
                "state": "CLOSED",
                "body": None,
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0].state == "closed"
    assert rows[0].body == ""
    assert rows[0].url.endswith("/issues/3")


def test_pull_request_url_is_not_an_issue() -> None:
    with pytest.raises(SystemExit, match="issue url"):
        beads.issues_from_payload(
            [
                {
                    "number": 1,
                    "title": "a pull request",
                    "url": "https://github.com/stuagano/apx-agent/pull/1",
                    "state": "closed",
                }
            ]
        )


def _issue(number: int, state: str) -> beads.IssueSnapshot:
    return beads.IssueSnapshot(
        number,
        "title",
        "body",
        f"https://github.com/stuagano/apx-agent/issues/{number}",
        state,
    )


def test_closed_issue_force_closes_the_matching_bead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beads, "find_bead_row", lambda _cwd, _url: {"id": "apx-om5", "status": "open"})
    seen: dict[str, str] = {}

    def fake_close(
        _cwd: Path,
        bead_id: str,
        reason: str,
        *,
        dry_run: bool,
        force: bool = False,
    ) -> str:
        seen["bead_id"] = bead_id
        seen["reason"] = reason
        seen["force"] = "true" if force else "false"
        seen["dry_run"] = "true" if dry_run else "false"
        return "closed"

    monkeypatch.setattr(beads, "close_bead", fake_close)
    result = beads.apply_issue_sync(Path("."), _issue(12, "closed"), dry_run=False)
    assert result == beads.IssueSync("apx-om5", "closed")
    assert seen["bead_id"] == "apx-om5"
    assert seen["reason"] == "GitHub issue #12 is closed"
    assert seen["force"] == "true"
    assert seen["dry_run"] == "false"


def test_open_issue_reopens_a_closed_bead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        beads, "find_bead_row", lambda _cwd, _url: {"id": "apx-om5", "status": "closed"}
    )
    seen: dict[str, str] = {}

    def fake_reopen(_cwd: Path, bead_id: str, reason: str, *, dry_run: bool) -> str:
        del dry_run
        seen["bead_id"] = bead_id
        seen["reason"] = reason
        return "reopened"

    monkeypatch.setattr(beads, "reopen_bead", fake_reopen)
    result = beads.apply_issue_sync(Path("."), _issue(12, "open"), dry_run=False)
    assert result.outcome == "reopened"
    assert seen["reason"] == "GitHub issue #12 is open"


def test_open_issue_leaves_in_progress_and_skips_a_missing_closed_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        beads, "find_bead_row", lambda _cwd, _url: {"id": "apx-om5", "status": "in_progress"}
    )

    def forbidden(*_args: str, **_kwargs: str) -> str:
        raise AssertionError("should not write")

    monkeypatch.setattr(beads, "close_bead", forbidden)
    monkeypatch.setattr(beads, "reopen_bead", forbidden)
    monkeypatch.setattr(beads, "create_bead", forbidden)
    open_result = beads.apply_issue_sync(Path("."), _issue(4, "open"), dry_run=False)
    assert open_result.outcome == "unchanged"
    monkeypatch.setattr(beads, "find_bead_row", lambda _cwd, _url: None)
    closed_result = beads.apply_issue_sync(Path("."), _issue(4, "closed"), dry_run=False)
    assert closed_result == beads.IssueSync("", "skipped")


def test_sync_open_rejects_a_missing_state_and_a_full_page(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    missing.write_text(
        '[{"number": 1, "title": "t", "url": "https://github.com/stuagano/apx-agent/issues/1"}]',
        encoding="utf-8",
    )
    no_state = subprocess.run(
        [sys.executable, str(BEADS_PY), "sync-open", "--issues-file", str(missing)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert no_state.returncode != 0
    assert "state" in no_state.stderr
    full = tmp_path / "full.json"
    full.write_text(
        '[{"number": 1, "title": "t", "url": "https://github.com/stuagano/apx-agent/issues/1", "state": "closed"}]',
        encoding="utf-8",
    )
    capped = subprocess.run(
        [
            sys.executable,
            str(BEADS_PY),
            "sync-open",
            "--issues-file",
            str(full),
            "--limit",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert capped.returncode != 0
    assert "limit" in capped.stderr


def test_sync_issue_rejects_pull_requests_and_missing_args(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        '{"action": "closed", "issue": {"number": 9, "title": "pr", "html_url": '
        '"https://github.com/stuagano/apx-agent/pull/9", "state": "closed", "pull_request": {}}}',
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [sys.executable, str(BEADS_PY), "sync-issue", "--event", str(event)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "pull request" in rejected.stderr
    bare = subprocess.run(
        [sys.executable, str(BEADS_PY), "sync-issue"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert bare.returncode != 0
    assert "--state" in bare.stderr


def test_workflows_sync_issue_state_with_the_pinned_bd() -> None:
    issue = (ROOT / ".github" / "workflows" / "beads-issue.yml").read_text(encoding="utf-8")
    assert "types: [opened, closed, reopened]" in issue
    assert "group: beads-hive" in issue
    assert "bash scripts/beads/setup_bd.sh" in issue
    assert 'beads.py sync-issue --event "$GITHUB_EVENT_PATH"' in issue
    daily = (ROOT / ".github" / "workflows" / "beads-sync.yml").read_text(encoding="utf-8")
    assert "group: beads-hive" in daily
    assert "bash scripts/beads/setup_bd.sh" in daily
    assert "--state all" in daily
    assert "number,title,body,url,state" in daily
    assert "beads.py sync-open --issues-file" in daily
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "external_ref is the issue URL" in agents
    assert "already closed or reopened" in agents
