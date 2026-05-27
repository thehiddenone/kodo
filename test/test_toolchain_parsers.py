"""Behavior tests for pytest and vitest output parsers.

Tests verify that parsing real-world output strings produces the expected
ToolchainTestResult structure — pass/fail counts and per-test case details.
No subprocesses are spawned.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from kodo.toolchains._interface import ToolchainTestResult
from kodo.toolchains.node._vitest import parse_vitest_stdout
from kodo.toolchains.python._pytest import parse_pytest_json, parse_pytest_stdout

# ---------------------------------------------------------------------------
# parse_pytest_stdout
# ---------------------------------------------------------------------------


def test_parse_pytest_stdout_all_passed() -> None:
    """
    Given pytest output with only passing tests,
    when parse_pytest_stdout is called,
    then passed count equals the total and failed is zero.
    """
    stdout = "3 passed in 0.12s"
    result = parse_pytest_stdout(stdout, "")
    assert result.passed == 3
    assert result.failed == 0


def test_parse_pytest_stdout_mixed_results() -> None:
    """
    Given pytest output with both passed and failed tests,
    when parse_pytest_stdout is called,
    then both counts are extracted correctly.
    """
    stdout = "5 passed, 2 failed in 0.34s"
    result = parse_pytest_stdout(stdout, "")
    assert result.passed == 5
    assert result.failed == 2


def test_parse_pytest_stdout_all_failed() -> None:
    """
    Given pytest output where all tests fail,
    when parse_pytest_stdout is called,
    then passed is zero and failed matches the count.
    """
    stdout = "3 failed in 0.10s"
    result = parse_pytest_stdout(stdout, "")
    assert result.failed == 3
    assert result.passed == 0


def test_parse_pytest_stdout_empty_output_returns_zero_counts() -> None:
    """
    Given empty stdout and stderr,
    when parse_pytest_stdout is called,
    then both counts are zero and cases list is empty.
    """
    result = parse_pytest_stdout("", "")
    assert result.passed == 0
    assert result.failed == 0
    assert result.cases == []


def test_parse_pytest_stdout_error_counts_as_failure() -> None:
    """
    Given pytest output that reports errors (not just failures),
    when parse_pytest_stdout is called,
    then errors increment the failed count.
    """
    stdout = "2 passed, 1 error in 0.20s"
    result = parse_pytest_stdout(stdout, "")
    assert result.failed == 1
    assert result.passed == 2


def test_parse_pytest_stdout_extracts_failed_case_names() -> None:
    """
    Given pytest verbose output with a FAILED line,
    when parse_pytest_stdout is called,
    then a failing case entry with the right name appears.
    """
    stdout = (
        "FAILED tests/test_foo.py::test_something - AssertionError\n2 passed, 1 failed in 0.05s"
    )
    result = parse_pytest_stdout(stdout, "")
    failed_cases = [c for c in result.cases if not c.passed]
    assert len(failed_cases) == 1
    assert "test_foo.py::test_something" in failed_cases[0].name


def test_parse_pytest_stdout_extracts_passed_case_names() -> None:
    """
    Given pytest output with a PASSED line,
    when parse_pytest_stdout is called,
    then a passing case entry with the right name appears.
    """
    stdout = "PASSED tests/test_bar.py::test_ok\n1 passed in 0.02s"
    result = parse_pytest_stdout(stdout, "")
    passed_cases = [c for c in result.cases if c.passed]
    assert len(passed_cases) == 1
    assert "test_bar.py::test_ok" in passed_cases[0].name


def test_parse_pytest_stdout_uses_stderr_as_fallback() -> None:
    """
    Given no useful information in stdout but summary in stderr,
    when parse_pytest_stdout is called,
    then the stderr summary is parsed correctly.
    """
    result = parse_pytest_stdout("", "4 passed in 0.08s")
    assert result.passed == 4


def test_parse_pytest_stdout_returns_toolchain_test_result() -> None:
    """
    Given any pytest output,
    when parse_pytest_stdout is called,
    then the return value is a ToolchainTestResult instance.
    """
    result = parse_pytest_stdout("1 passed in 0.01s", "")
    assert isinstance(result, ToolchainTestResult)


def test_parse_pytest_stdout_all_passed_reports_no_failures() -> None:
    """
    Given a result with all tests passing,
    when all_passed is checked,
    then it returns True.
    """
    result = parse_pytest_stdout("5 passed in 0.1s", "")
    assert result.all_passed


def test_parse_pytest_stdout_failed_tests_all_passed_is_false() -> None:
    """
    Given a result with at least one failure,
    when all_passed is checked,
    then it returns False.
    """
    result = parse_pytest_stdout("2 passed, 1 failed in 0.1s", "")
    assert not result.all_passed


# ---------------------------------------------------------------------------
# parse_pytest_json
# ---------------------------------------------------------------------------


def _write_json_report(data: dict[str, object]) -> Path:
    """Write a JSON report dict to a temp file and return its path."""
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        json.dump(data, tmp)
        return Path(tmp.name)


def test_parse_pytest_json_all_passed() -> None:
    """
    Given a JSON report with only passing tests,
    when parse_pytest_json is called,
    then passed equals the total test count and failed is zero.
    """
    data = {
        "summary": {"passed": 3},
        "tests": [
            {"nodeid": "test_a::test_one", "outcome": "passed"},
            {"nodeid": "test_a::test_two", "outcome": "passed"},
            {"nodeid": "test_a::test_three", "outcome": "passed"},
        ],
    }
    path = _write_json_report(data)
    result = parse_pytest_json(path)
    assert result.passed == 3
    assert result.failed == 0


def test_parse_pytest_json_mixed_results() -> None:
    """
    Given a JSON report with passing and failing tests,
    when parse_pytest_json is called,
    then both counts are correct.
    """
    data = {
        "summary": {"passed": 2, "failed": 1},
        "tests": [
            {"nodeid": "t::a", "outcome": "passed"},
            {"nodeid": "t::b", "outcome": "passed"},
            {"nodeid": "t::c", "outcome": "failed", "call": {"longrepr": "AssertionError"}},
        ],
    }
    path = _write_json_report(data)
    result = parse_pytest_json(path)
    assert result.passed == 2
    assert result.failed == 1


def test_parse_pytest_json_case_names_preserved() -> None:
    """
    Given a JSON report with named tests,
    when parse_pytest_json is called,
    then each case's name matches the nodeid.
    """
    data = {
        "summary": {"passed": 1},
        "tests": [{"nodeid": "module::test_func", "outcome": "passed"}],
    }
    path = _write_json_report(data)
    result = parse_pytest_json(path)
    assert len(result.cases) == 1
    assert result.cases[0].name == "module::test_func"


def test_parse_pytest_json_failed_case_has_message() -> None:
    """
    Given a JSON report with a failing test that has a longrepr,
    when parse_pytest_json is called,
    then the failing case's message contains the failure detail.
    """
    data = {
        "summary": {"failed": 1},
        "tests": [
            {
                "nodeid": "t::bad",
                "outcome": "failed",
                "call": {"longrepr": "AssertionError: expected 1 got 2"},
            }
        ],
    }
    path = _write_json_report(data)
    result = parse_pytest_json(path)
    failed = [c for c in result.cases if not c.passed]
    assert len(failed) == 1
    assert "AssertionError" in failed[0].message


def test_parse_pytest_json_error_counted_as_failure() -> None:
    """
    Given a JSON report summary with errors,
    when parse_pytest_json is called,
    then errors are added to the failed count.
    """
    data = {"summary": {"passed": 1, "error": 2}, "tests": []}
    path = _write_json_report(data)
    result = parse_pytest_json(path)
    assert result.failed == 2


def test_parse_pytest_json_empty_tests_list() -> None:
    """
    Given a JSON report with no test entries,
    when parse_pytest_json is called,
    then cases list is empty.
    """
    data = {"summary": {"passed": 0}, "tests": []}
    path = _write_json_report(data)
    result = parse_pytest_json(path)
    assert result.cases == []


# ---------------------------------------------------------------------------
# parse_vitest_stdout
# ---------------------------------------------------------------------------


def test_parse_vitest_stdout_all_passed() -> None:
    """
    Given vitest output with all tests passing,
    when parse_vitest_stdout is called,
    then passed count matches and failed is zero.
    """
    stdout = "Tests  5 passed"
    result = parse_vitest_stdout(stdout, "")
    assert result.passed == 5
    assert result.failed == 0


def test_parse_vitest_stdout_mixed_results() -> None:
    """
    Given vitest output with passing and failing tests,
    when parse_vitest_stdout is called,
    then both counts are extracted from the summary line.
    """
    stdout = "Tests  3 passed | 2 failed"
    result = parse_vitest_stdout(stdout, "")
    assert result.passed == 3
    assert result.failed == 2


def test_parse_vitest_stdout_empty_output() -> None:
    """
    Given empty vitest output,
    when parse_vitest_stdout is called,
    then both counts are zero.
    """
    result = parse_vitest_stdout("", "")
    assert result.passed == 0
    assert result.failed == 0


def test_parse_vitest_stdout_checkmark_lines_become_passed_cases() -> None:
    """
    Given vitest output with checkmark (✓) lines,
    when parse_vitest_stdout is called,
    then those become passing case entries.
    """
    stdout = "✓ should compute total\n✓ should handle edge case\nTests  2 passed"
    result = parse_vitest_stdout(stdout, "")
    passed_cases = [c for c in result.cases if c.passed]
    assert len(passed_cases) == 2


def test_parse_vitest_stdout_cross_lines_become_failed_cases() -> None:
    """
    Given vitest output with cross (×) lines,
    when parse_vitest_stdout is called,
    then those become failing case entries.
    """
    stdout = "× should not fail\nTests  0 passed | 1 failed"
    result = parse_vitest_stdout(stdout, "")
    failed_cases = [c for c in result.cases if not c.passed]
    assert len(failed_cases) == 1


def test_parse_vitest_stdout_uses_stderr() -> None:
    """
    Given summary only in stderr,
    when parse_vitest_stdout is called,
    then it is parsed correctly.
    """
    result = parse_vitest_stdout("", "Tests  4 passed")
    assert result.passed == 4


def test_parse_vitest_stdout_returns_toolchain_test_result() -> None:
    """
    Given any vitest output,
    when parse_vitest_stdout is called,
    then the return value is a ToolchainTestResult instance.
    """
    result = parse_vitest_stdout("Tests  1 passed", "")
    assert isinstance(result, ToolchainTestResult)


def test_parse_vitest_stdout_all_passed_property() -> None:
    """
    Given all tests pass in vitest output,
    when all_passed is checked on the result,
    then it returns True.
    """
    result = parse_vitest_stdout("Tests  3 passed", "")
    assert result.all_passed


def test_parse_vitest_stdout_fail_lines_with_alt_symbol() -> None:
    """
    Given vitest output using the 'FAIL' prefix for failures,
    when parse_vitest_stdout is called,
    then those lines create failing cases.
    """
    stdout = "FAIL src/calc.test.ts\nTests  0 passed | 1 failed"
    result = parse_vitest_stdout(stdout, "")
    failed_cases = [c for c in result.cases if not c.passed]
    assert len(failed_cases) == 1
