"""vitest stdout output parser."""

from __future__ import annotations

from kodo.toolchains._interface import TestCase, TestResult

__all__ = ["parse_vitest_stdout"]


def parse_vitest_stdout(stdout: str, stderr: str) -> TestResult:
    """Parse vitest terminal output into a :class:`TestResult`.

    Reads the summary line (e.g. ``Tests  5 passed | 2 failed``) and
    individual ``✓``/``×`` lines from the combined output.

    Args:
        stdout (str): Standard output from the vitest process.
        stderr (str): Standard error from the vitest process.

    Returns:
        TestResult: Best-effort structured outcome.
    """
    combined = stdout + "\n" + stderr
    passed = 0
    failed = 0
    cases: list[TestCase] = []

    for line in combined.splitlines():
        stripped = line.strip()

        # Summary line: "Tests  5 passed | 2 failed"
        if stripped.startswith("Tests") and ("passed" in stripped or "failed" in stripped):
            for segment in stripped.split("|"):
                seg = segment.strip()
                parts = seg.split()
                if len(parts) >= 2:
                    try:
                        count = int(parts[0])
                    except ValueError:
                        continue
                    label = parts[1].lower()
                    if "passed" in label:
                        passed = count
                    elif "failed" in label:
                        failed = count

        # Individual pass/fail markers
        if stripped.startswith("✓") or stripped.startswith("√"):
            name = stripped[1:].strip()
            cases.append(TestCase(name=name, passed=True))
        elif stripped.startswith("×") or stripped.startswith("✗") or stripped.startswith("FAIL"):
            name = stripped[1:].strip()
            cases.append(TestCase(name=name, passed=False, message=stripped))

    return TestResult(passed=passed, failed=failed, cases=cases)
