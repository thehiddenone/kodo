"""pytest JSON-report output parser."""

from __future__ import annotations

import json
from pathlib import Path

from kodo.toolchains._interface import TestCase, TestResult

__all__ = ["parse_pytest_json", "parse_pytest_stdout"]


def parse_pytest_json(report_path: Path) -> TestResult:
    """Parse a pytest ``--json-report`` output file into a :class:`TestResult`.

    Args:
        report_path (Path): Path to the ``.report.json`` file produced by
            ``pytest-json-report``.

    Returns:
        TestResult: Structured test outcome.
    """
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    summary = raw.get("summary", {})
    passed_count = int(summary.get("passed", 0))
    failed_count = int(summary.get("failed", 0)) + int(summary.get("error", 0))

    cases: list[TestCase] = []
    for test in raw.get("tests", []):
        node_id = str(test.get("nodeid", ""))
        outcome = str(test.get("outcome", "passed"))
        passed = outcome == "passed"
        message = ""
        if not passed:
            call_info = test.get("call", {})
            message = str(call_info.get("longrepr", "")) if call_info else ""
        cases.append(TestCase(name=node_id, passed=passed, message=message))

    return TestResult(passed=passed_count, failed=failed_count, cases=cases)


def parse_pytest_stdout(stdout: str, stderr: str) -> TestResult:
    """Parse pytest terminal output into a :class:`TestResult`.

    Used as fallback when ``pytest-json-report`` is not available.  Extracts
    the summary line (e.g. ``5 passed, 2 failed``) from the last lines of
    combined output.

    Args:
        stdout (str): Standard output from the pytest process.
        stderr (str): Standard error from the pytest process.

    Returns:
        TestResult: Best-effort structured outcome; individual case details
        are populated from ``FAILED`` lines in the output.
    """
    combined = stdout + "\n" + stderr
    passed = 0
    failed = 0
    cases: list[TestCase] = []

    for line in reversed(combined.splitlines()):
        line = line.strip()
        if " passed" in line or " failed" in line:
            # e.g. "5 passed, 2 failed in 0.12s"
            for segment in line.split(","):
                parts = segment.strip().split()[0:2]
                if len(parts) == 2:
                    count_str, label = parts
                    try:
                        count = int(count_str)
                    except ValueError:
                        continue
                    if "passed" in label:
                        passed = count
                    elif "failed" in label or "error" in label:
                        failed = count
            break

    for line in combined.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            name = stripped[len("FAILED ") :].split(" - ")[0].strip()
            message = stripped
            cases.append(TestCase(name=name, passed=False, message=message))
        elif stripped.startswith("PASSED "):
            name = stripped[len("PASSED ") :].strip()
            cases.append(TestCase(name=name, passed=True))

    return TestResult(passed=passed, failed=failed, cases=cases)
