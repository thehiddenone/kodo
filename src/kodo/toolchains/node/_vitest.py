"""vitest stdout output parser."""

from __future__ import annotations

from kodo.toolchains._interface import ToolchainTestCase, ToolchainTestResult

__all__ = ["parse_vitest_stdout"]


def parse_vitest_stdout(stdout: str, stderr: str) -> ToolchainTestResult:
    """Parse vitest terminal output into a :class:`ToolchainTestResult`.

    Reads the summary line (e.g. ``Tests  5 passed | 2 failed``) and
    individual ``✓``/``×`` lines from the combined output.

    Args:
        stdout (str): Standard output from the vitest process.
        stderr (str): Standard error from the vitest process.

    Returns:
        ToolchainTestResult: Best-effort structured outcome.
    """
    combined = stdout + "\n" + stderr
    passed = 0
    failed = 0
    cases: list[ToolchainTestCase] = []

    for line in combined.splitlines():
        stripped = line.strip()

        # Summary line: "Tests  5 passed | 2 failed"
        if stripped.startswith("Tests") and ("passed" in stripped or "failed" in stripped):
            for segment in stripped.split("|"):
                parts = segment.strip().split()
                for i, token in enumerate(parts[:-1]):
                    try:
                        count = int(token)
                    except ValueError:
                        continue
                    label = parts[i + 1].lower()
                    if "passed" in label:
                        passed = count
                        break
                    elif "failed" in label:
                        failed = count
                        break

        # Individual pass/fail markers
        if stripped.startswith("✓") or stripped.startswith("√"):
            name = stripped[1:].strip()
            cases.append(ToolchainTestCase(name=name, passed=True))
        elif stripped.startswith("×") or stripped.startswith("✗") or stripped.startswith("FAIL"):
            name = stripped[1:].strip()
            cases.append(ToolchainTestCase(name=name, passed=False, message=stripped))

    return ToolchainTestResult(passed=passed, failed=failed, cases=cases)
