"""The pull-request bead check, issue-state sync, the bd pin, and hive push retry."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
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
    assert "group: beads-issue-${{ github.run_id }}" in issue
    assert "cancel-in-progress: false" in issue
    assert "bash scripts/beads/setup_bd.sh" in issue
    assert 'beads.py sync-issue --event "$GITHUB_EVENT_PATH"' in issue
    daily = (ROOT / ".github" / "workflows" / "beads-sync.yml").read_text(encoding="utf-8")
    assert "group: beads-sync-${{ github.run_id }}" in daily
    assert "cancel-in-progress: false" in daily
    assert "workflow_dispatch" in daily
    assert 'cron: "17 6 * * *"' in daily
    assert "bash scripts/beads/setup_bd.sh" in daily
    assert "--state all" in daily
    assert "number,title,body,url,state" in daily
    assert "beads.py sync-open --issues-file" in daily
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "external_ref is the issue URL" in agents
    assert "already closed or reopened" in agents


def test_merge_workflow_catches_token_squash_without_dropping_runs() -> None:
    merge = (ROOT / ".github" / "workflows" / "beads-merge.yml").read_text(encoding="utf-8")
    assert "pull_request_target:" not in merge
    assert "types: [closed]" in merge
    assert "workflow_dispatch" in merge
    assert 'cron: "14,29,44,59 * * * *"' in merge
    assert "group: beads-merge-${{ github.run_id }}" in merge
    assert "cancel-in-progress: false" in merge
    assert "issues: write" in merge
    assert "close-merged" in merge
    assert "--within-hours 12" in merge
    assert 'beads.py close-pr --event "$GITHUB_EVENT_PATH"' in merge


def test_closing_keywords_find_issue_numbers() -> None:
    assert beads.closing_issue_numbers("Fixes #1233\n") == [1233]
    assert beads.closing_issue_numbers("Closes #1237.") == [1237]
    assert beads.closing_issue_numbers("Resolves: #4 and closes #5") == [4, 5]
    assert beads.closing_issue_numbers("Fixes #1\nfixes #1") == [1]
    assert beads.closing_issue_numbers(
        "Fixes https://github.com/stuagano/apx-agent/issues/1233"
    ) == [1233]
    assert beads.closing_issue_numbers("fixed in #12") == []
    assert beads.closing_issue_numbers("see #9 and apx-om5") == []
    assert beads.closing_issue_numbers("prefixFixes #3") == []


def test_only_intake_hold_gates_force_a_merge_close() -> None:
    gate = {
        "issue_type": "gate",
        "status": "open",
        "dependency_type": "blocks",
        "description": "Ad-hoc gate blocking apx-n0o\n\nReason: intake-hold: external report, never work",
    }
    closed_gate = {
        "issue_type": "gate",
        "status": "closed",
        "dependency_type": "blocks",
        "description": "Reason: intake-hold: external report, never work",
    }
    tracked = {
        "issue_type": "task",
        "status": "open",
        "dependency_type": "tracks",
        "description": "Reason: intake-hold: external report, never work",
    }
    task = {
        "issue_type": "task",
        "status": "open",
        "dependency_type": "blocks",
        "description": "still open work",
    }
    assert beads.only_intake_hold_blockers([gate, tracked, closed_gate]) is True
    assert beads.only_intake_hold_blockers([gate, task]) is False
    assert beads.only_intake_hold_blockers([tracked]) is False
    assert beads.only_intake_hold_blockers([]) is False


def test_merge_close_force_reads_show_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        beads,
        "show_bead",
        lambda _cwd, _bead_id: {
            "status": "open",
            "dependencies": [
                {
                    "id": "apx-abl",
                    "issue_type": "gate",
                    "status": "open",
                    "dependency_type": "blocks",
                    "description": "Reason: intake-hold: external report, never work",
                }
            ],
        },
    )
    assert beads.merge_close_force(Path("."), "apx-n0o") is True


def test_close_pull_request_forces_an_intake_hold_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beads, "merge_close_force", lambda _cwd, _bead_id: True)
    seen: dict[str, bool] = {}

    def fake_close(
        _cwd: Path,
        bead_id: str,
        reason: str,
        *,
        dry_run: bool,
        force: bool = False,
    ) -> str:
        del bead_id, reason, dry_run
        seen["force"] = force
        return "closed"

    monkeypatch.setattr(beads, "close_bead", fake_close)
    pr = beads.PrSnapshot(
        1234,
        "Move eval assets",
        "apx-n0o",
        "cursor/apx-n0o-eval-judge-assets-bdd3",
    )
    assert beads.close_pull_request(Path("."), pr, "", "Merged PR #1234", dry_run=False) is True
    assert seen["force"] is True


def test_blocking_ids_keep_open_block_edges() -> None:
    row = {
        "dependencies": [
            {"id": "apx-abl", "status": "open", "dependency_type": "blocks"},
            {"id": "apx-q7f", "status": "closed", "dependency_type": "blocks"},
            {"id": "apx-tracks", "status": "open", "dependency_type": "tracks"},
        ]
    }
    assert beads.blocking_ids(row) == ["apx-abl"]
    assert beads.blocking_ids({}) == []


def test_force_close_logs_the_blocker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        beads,
        "show_bead",
        lambda _cwd, _bead_id: {
            "status": "open",
            "dependencies": [
                {"id": "apx-abl", "status": "open", "dependency_type": "blocks"},
                {"id": "apx-q7f", "status": "closed", "dependency_type": "blocks"},
            ],
        },
    )

    def fake_run(args: list[str], *, cwd: Path, check: bool) -> beads.CmdResult:
        del cwd, check
        assert args[-1] == "--force"
        return beads.CmdResult(0, "", "")

    monkeypatch.setattr(beads, "_run", fake_run)
    outcome = beads.close_bead(Path("."), "apx-n0o", "Merged PR #1234", dry_run=False, force=True)
    assert outcome == "closed"
    logged = capsys.readouterr().out
    assert "apx-n0o is blocked by [apx-abl]; closing with --force" in logged
    assert "apx-q7f" not in logged


def test_fixes_keyword_closes_the_issue_bead_when_the_title_has_no_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_find(_cwd: Path, url: str) -> str | None:
        if url.endswith("/issues/1233"):
            return "apx-pry"
        return None

    monkeypatch.setattr(beads, "find_bead", fake_find)
    monkeypatch.setattr(beads, "merge_close_force", lambda _cwd, _bead_id: False)
    seen: dict[str, str | bool] = {}

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
        seen["force"] = force
        seen["dry_run"] = dry_run
        return "closed"

    monkeypatch.setattr(beads, "close_bead", fake_close)
    notes: list[tuple[str, int, str]] = []

    def fake_issue(repository: str, number: int, note: str) -> str:
        notes.append((repository, number, note))
        return "closed"

    monkeypatch.setattr(beads, "close_linked_issue", fake_issue)
    pr = beads.PrSnapshot(
        1242,
        "Seed deploy-time app: constants from app_state (#1233)",
        "Fixes #1233\n",
        "cursor/app-state-constants-1610",
    )
    changed = beads.close_pull_request(
        Path("."),
        pr,
        "stuagano/apx-agent",
        "Merged PR #1242",
        dry_run=False,
    )
    assert changed is True
    assert seen["bead_id"] == "apx-pry"
    assert seen["force"] is False
    assert seen["reason"] == "Merged PR #1242"
    assert notes == [
        (
            "stuagano/apx-agent",
            1233,
            "Closed by merged PR #1242: Seed deploy-time app: constants from app_state (#1233)",
        )
    ]


def test_select_recent_prs_drops_merges_outside_the_window() -> None:
    now = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
    rows = beads.select_recent_prs(
        [
            {
                "number": 1247,
                "title": "after the floor",
                "body": "Fixes #1",
                "headRefName": "cursor/after-the-floor",
                "mergedAt": "2026-11-23T17:51:00Z",
            },
            {
                "number": 1246,
                "title": "floor explanation",
                "body": "names the #911 work",
                "headRefName": "cursor/beads-reconcile-f7e5",
                "mergedAt": "2026-11-23T17:49:08Z",
            },
            {
                "number": 1242,
                "title": "Seed app_state",
                "body": "Fixes #1233",
                "headRefName": "cursor/app-state-constants-1610",
                "mergedAt": "2026-11-23T17:16:53Z",
            },
            {
                "number": 1230,
                "title": "caller identity",
                "body": None,
                "headRefName": "cursor/apx-gar-caller-identity-blocker-50f5",
                "mergedAt": "2026-11-23T02:59:53Z",
            },
            {
                "number": 1236,
                "title": "docs: name the UI step",
                "body": "Refs #911\napx-e0i",
                "headRefName": "cursor/unity-gateway-guardrail-docs-8a76",
                "mergedAt": "2026-11-23T16:49:41Z",
            },
        ],
        within_hours=12,
        now=now,
    )
    assert [pr.number for pr in rows] == [1247, 1246, 1242, 1236]
    assert rows[0].body == "Fixes #1"
    later = beads.select_recent_prs(
        [
            {
                "number": 1300,
                "title": "later work",
                "body": "",
                "headRefName": "cursor/later-work",
                "mergedAt": "2026-11-24T02:00:00Z",
            },
            {
                "number": 1299,
                "title": "just outside",
                "body": "",
                "headRefName": "cursor/just-outside",
                "mergedAt": "2026-11-23T23:30:00Z",
            },
        ],
        within_hours=12,
        now=datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc),
    )
    assert [pr.number for pr in later] == [1300]
    with pytest.raises(SystemExit, match="within-hours"):
        beads.select_recent_prs([], within_hours=-1, now=now)


@pytest.mark.parametrize("within_hours", [0, 12])
def test_skip_list_keeps_the_911_bead_open_under_any_window(
    monkeypatch: pytest.MonkeyPatch, within_hours: int
) -> None:
    monkeypatch.setattr(beads, "find_bead", lambda _cwd, _url: None)
    monkeypatch.setattr(beads, "merge_close_force", lambda _cwd, _bead_id: False)
    monkeypatch.setattr(beads, "close_linked_issue", lambda _repo, _number, _note: "closed")
    closed: list[tuple[str, str]] = []

    def fake_close(
        _cwd: Path,
        bead_id: str,
        reason: str,
        *,
        dry_run: bool,
        force: bool = False,
    ) -> str:
        closed.append((bead_id, reason))
        return "closed"

    monkeypatch.setattr(beads, "close_bead", fake_close)
    payload = [
        {
            "number": 1250,
            "title": "Normal work (apx-n0r)",
            "body": "",
            "headRefName": "cursor/normal-work",
            "mergedAt": "2026-11-24T11:00:00Z",
        },
        {
            "number": 1246,
            "title": "Force-close a merged bead only when intake-hold gates are the blockers (apx-9rz)",
            "body": "`#1236` names `apx-e0i` as a docs reference",
            "headRefName": "cursor/beads-reconcile-f7e5",
            "mergedAt": "2026-11-24T10:00:00Z",
        },
        {
            "number": 1236,
            "title": "docs: name the UI step for Unity Gateway external guardrails",
            "body": "Refs #911\napx-e0i",
            "headRefName": "cursor/unity-gateway-guardrail-docs-8a76",
            "mergedAt": "2026-11-24T09:00:00Z",
        },
    ]
    prs = beads.select_recent_prs(
        payload,
        within_hours=within_hours,
        now=datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc),
    )
    assert [pr.number for pr in prs] == [1250, 1246, 1236]
    changed = [
        beads.close_pull_request(Path("."), pr, "stuagano/apx-agent", f"Merged PR #{pr.number}", dry_run=False)
        for pr in prs
    ]
    assert changed == [True, True, False]
    assert closed == [("apx-n0r", "Merged PR #1250"), ("apx-9rz", "Merged PR #1246")]
    other = beads.PrSnapshot(1251, "Finish the guardrail (apx-e0i)", "", "cursor/finish")
    assert beads.close_pull_request(Path("."), other, "stuagano/apx-agent", "Merged PR #1251", dry_run=False)
    assert closed[-1] == ("apx-e0i", "Merged PR #1251")


def test_close_merged_ignores_pull_requests_outside_the_window(tmp_path: Path) -> None:
    path = tmp_path / "prs.json"
    path.write_text(
        '[{"number": 1230, "title": "caller identity", "body": "", '
        '"headRefName": "cursor/apx-gar-caller-identity-blocker-50f5", '
        '"mergedAt": "2020-01-01T00:00:00Z"}]',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(BEADS_PY),
            "close-merged",
            "--prs-file",
            str(path),
            "--within-hours",
            "12",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "no merged pull requests in the window" in proc.stdout
