"""The run primitive must fail on a missing, empty, or wrong file and pass on a real one."""

from __future__ import annotations

import json

import pytest

from ctk import Artifact, Checklist, VerificationError, claim_vs_reality, verify
from ctk.gate import Check, CheckFailure


def test_check_passes_when_the_predicate_holds() -> None:
    Check("rows", "3", lambda: None).run()


def test_check_raises_expected_against_found() -> None:
    with pytest.raises(CheckFailure) as exc:
        Check("rows", "3", lambda: "0").run()
    assert exc.value.name == "rows"
    assert exc.value.expected == "3"
    assert exc.value.found == "0"
    assert "expected 3, found 0" in str(exc.value)


def test_missing_file_fails(tmp_path) -> None:
    path = tmp_path / "missing.json"
    with pytest.raises(CheckFailure) as exc:
        Artifact(str(path)).check()
    assert "does not exist" in exc.value.found


def test_empty_file_fails(tmp_path) -> None:
    path = tmp_path / "empty.json"
    path.write_text("")
    with pytest.raises(CheckFailure) as exc:
        Artifact(str(path)).check()
    assert "EMPTY" in exc.value.found


def test_real_file_passes_and_reads_back(tmp_path) -> None:
    path = tmp_path / "out.json"
    path.write_text(json.dumps({"order": 7}))
    art = Artifact(
        str(path), is_json=True, json_keys=["order"], must_contain="order",
    ).check()
    assert json.loads(art.read())["order"] == 7
    assert art.value()["order"] == 7


def test_verify_reports_every_problem(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(VerificationError) as exc:
        verify(
            Artifact(str(tmp_path / "missing.json")),
            Artifact(str(bad), json_keys=["order"]),
        )
    report = str(exc.value)
    assert "does not exist" in report
    assert "order" in report


def test_claim_and_checklist_use_the_same_gate(tmp_path) -> None:
    path = tmp_path / "rows.csv"
    path.write_text("id\n1\n2\n")
    claim_vs_reality({"rows": str(path)}, must_contain={"rows": "id"})
    Artifact(str(path), rows=2).check()

    cl = Checklist()
    cl.file_exists(str(path)).exit_code(0).verify()
    cl.that(False, "forced miss")
    with pytest.raises(VerificationError):
        cl.verify()
