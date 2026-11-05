"""Non-writing tests for the required-mode JUnit execution contract (issue #826).

An all-skipped, empty, missing, failing, or incomplete required run must fail
closed.  Only a report whose expected cases all executed and passed is green.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.integration._disposable_postgres import (
    RequiredTestExecutionError,
    verify_required_test_report,
)

_CLASSNAME = "tests.example"


def _write_report(
    tmp_path: Path, cases: list[tuple[str, str]]
) -> Path:
    """Write a pytest-shaped JUnit report of ``(name, outcome)`` pairs."""
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for name, outcome in cases:
        case = ET.SubElement(
            suite, "testcase", {"classname": _CLASSNAME, "name": name}
        )
        if outcome == "skipped":
            ET.SubElement(case, "skipped")
        elif outcome == "failure":
            ET.SubElement(case, "failure", {"message": "boom"})
        elif outcome == "error":
            ET.SubElement(case, "error", {"message": "boom"})
    path = tmp_path / "report.xml"
    ET.ElementTree(root).write(path)
    return path


def test_all_passing_expected_cases_are_accepted(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path, [("test_one", "passed"), ("test_two", "passed")]
    )

    summary = verify_required_test_report(
        report, expected_names={"test_one", "test_two"}
    )

    assert summary.total == 2
    assert summary.passed == 2
    assert summary.skipped == 0
    assert summary.failed == 0
    assert summary.errors == 0


def test_missing_report_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RequiredTestExecutionError, match="report is missing"):
        verify_required_test_report(tmp_path / "absent.xml")


def test_empty_report_fails_closed(tmp_path: Path) -> None:
    report = _write_report(tmp_path, [])

    with pytest.raises(RequiredTestExecutionError, match="no test cases"):
        verify_required_test_report(report)


def test_all_skipped_report_fails_closed(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path, [("test_one", "skipped"), ("test_two", "skipped")]
    )

    with pytest.raises(RequiredTestExecutionError, match="was skipped"):
        verify_required_test_report(report)


def test_partially_skipped_report_fails_closed(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path, [("test_one", "passed"), ("test_two", "skipped")]
    )

    with pytest.raises(RequiredTestExecutionError, match="test_two was skipped"):
        verify_required_test_report(report)


def test_failed_case_fails_closed(tmp_path: Path) -> None:
    report = _write_report(tmp_path, [("test_one", "failure")])

    with pytest.raises(RequiredTestExecutionError, match="test_one failed"):
        verify_required_test_report(report)


def test_errored_case_fails_closed(tmp_path: Path) -> None:
    report = _write_report(tmp_path, [("test_one", "error")])

    with pytest.raises(RequiredTestExecutionError, match="test_one errored"):
        verify_required_test_report(report)


def test_missing_expected_case_fails_closed(tmp_path: Path) -> None:
    report = _write_report(tmp_path, [("test_one", "passed")])

    with pytest.raises(RequiredTestExecutionError, match="missing test cases"):
        verify_required_test_report(
            report, expected_names={"test_one", "test_two"}
        )


def test_unexpected_case_fails_closed(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path, [("test_one", "passed"), ("test_two", "passed")]
    )

    with pytest.raises(RequiredTestExecutionError, match="unexpected test cases"):
        verify_required_test_report(report, expected_names={"test_one"})


def test_duplicate_case_names_fail_the_expected_count(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path, [("test_one", "passed"), ("test_one", "passed")]
    )

    with pytest.raises(RequiredTestExecutionError, match="expected 1 test cases, collected 2"):
        verify_required_test_report(report, expected_names={"test_one"})


def test_minimum_executed_count_is_enforced(tmp_path: Path) -> None:
    report = _write_report(tmp_path, [("test_one", "passed")])

    with pytest.raises(RequiredTestExecutionError, match="at least 2 passing"):
        verify_required_test_report(report, minimum_executed=2)


def test_malformed_report_fails_closed(tmp_path: Path) -> None:
    report = tmp_path / "broken.xml"
    report.write_text("<testsuites><testcase", encoding="utf-8")

    with pytest.raises(RequiredTestExecutionError, match="could not be parsed"):
        verify_required_test_report(report)
