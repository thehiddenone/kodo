"""Score one agent-basics step (doc/AGENT_BUILDER.md §5.4.3).

``python3 /tests/check.py STEP`` writes ``/logs/verifier/reward.json`` with the
same three keys for every step, so Harbor's per-key mean across steps is
meaningful:

- ``outcome`` — the step's own result is correct;
- ``scope`` — nothing outside the step's ``tN/`` changed during the step,
  against the snapshot the step's ``setup.sh`` took;
- ``reward`` — both.

The checker reads only the final workspace files. It never reads a transcript
or a harness log and never names a tool (D31). The original files come from
rebuilding the fixture with this directory's own ``make_fixture.py``. A check
that raises scores 0; the reward file is always written.

Agent-written code (t5, t6, t7) runs in a subprocess with a timeout, so a hang
or a crash in it costs that step's ``outcome`` and nothing else.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_fixture  # noqa: E402

APP = make_fixture.APP
STATE = make_fixture.STATE
REWARD = Path(os.environ.get("BASICS_REWARD", "/logs/verifier/reward.json"))

_RUN_TIMEOUT = 60


def _changed(before: dict[str, str], after: dict[str, str], task: str, *, inside: bool) -> set[str]:
    """Paths inside (or outside) ``task/`` whose hash differs between snapshots."""
    return {
        path
        for path in set(before) | set(after)
        if path.startswith(f"{task}/") is inside and before.get(path) != after.get(path)
    }


def _only_changed(original: Path, task: str, allowed: set[str]) -> bool:
    """Whether the files under *task* differ from the original only in *allowed*."""
    before = {k: v for k, v in make_fixture.snapshot(original).items() if k.startswith(f"{task}/")}
    after = {k: v for k, v in make_fixture.snapshot(APP).items() if k.startswith(f"{task}/")}
    return _changed(before, after, task, inside=True) <= allowed


def _run_python(code: str, cwd: Path) -> object:
    """Run *code* in a fresh interpreter in *cwd*; return the JSON it prints."""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _call_cases(module: Path, function: str, cases: list[list[object]]) -> list[object]:
    """Call *function* from *module* on every case; ``"ValueError"`` marks a raise."""
    code = f"""
import importlib.util, json
spec = importlib.util.spec_from_file_location("m", {str(module)!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
out = []
for args in {cases!r}:
    try:
        out.append(getattr(m, {function!r})(*args))
    except ValueError:
        out.append("ValueError")
    except Exception as exc:
        out.append("error: " + type(exc).__name__)
print(json.dumps(out))
"""
    result = _run_python(code, module.parent)
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# One outcome check per step
# ---------------------------------------------------------------------------


def _s1(original: Path) -> bool:
    lines = (original / "t1" / "orders.csv").read_text().splitlines()[1:]
    rows = [line.split(",") for line in lines]
    top = max(rows, key=lambda row: int(row[2]))
    expected = f"rows={len(rows)}\nmax_id={top[0]}\n".encode()
    return (APP / "t1" / "summary.txt").read_bytes() == expected and _only_changed(
        original, "t1", {"t1/summary.txt"}
    )


def _s2(original: Path) -> bool:
    config = original / "t2" / "config"
    target = next(p for p in sorted(config.iterdir()) if "owner = legacy-team\n" in p.read_text())
    relative = f"t2/config/{target.name}"
    expected = target.read_bytes().replace(b"owner = legacy-team\n", b"owner = platform-team\n")
    return (APP / relative).read_bytes() == expected and _only_changed(original, "t2", {relative})


def _s3(original: Path) -> bool:
    printed = subprocess.run(
        [sys.executable, str(original / "t3" / "fingerprint.py")],
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT,
        check=True,
    ).stdout
    expected = printed.splitlines()[0] + "\n"
    return (APP / "t3" / "fingerprint.txt").read_bytes() == expected.encode() and _only_changed(
        original, "t3", {"t3/fingerprint.txt"}
    )


def _s4(original: Path) -> bool:
    expected = b"# generated-by: agent\nhello\n"
    return (APP / "t4" / "notes" / "hello.txt").read_bytes() == expected and _only_changed(
        original, "t4", {"t4/notes/hello.txt"}
    )


_PRICING_CASES: list[tuple[list[object], object]] = [
    ([1000, 10], 900),
    ([3, 50], 1),
    ([1, 50], 0),
    ([999, 15], 849),
    ([2999, 33], 2009),
    ([0, 30], 0),
    ([500, 0], 500),
    ([500, 100], 0),
    ([-1, 10], "ValueError"),
    ([100, -1], "ValueError"),
    ([100, 101], "ValueError"),
    ([100.0, 10], "ValueError"),
    ([100, 12.5], "ValueError"),
]


def _s5(original: Path) -> bool:
    got = _call_cases(
        APP / "t5" / "pricing.py", "apply_discount", [args for args, _ in _PRICING_CASES]
    )
    return got == [want for _, want in _PRICING_CASES] and _only_changed(
        original, "t5", {"t5/pricing.py"}
    )


_SLUG_CASES: list[tuple[list[object], object]] = [
    (["Hello, World!"], "hello-world"),
    (["  A  B  "], "a-b"),
    (["x__y--z"], "x-y-z"),
    (["Version 3.10"], "version-3-10"),
    (["---"], ""),
]


def _s6(original: Path) -> bool:
    if not _only_changed(original, "t6", {"t6/slug.py"}):
        return False
    got = _call_cases(APP / "t6" / "slug.py", "slugify", [args for args, _ in _SLUG_CASES])
    if got != [want for _, want in _SLUG_CASES]:
        return False
    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=APP / "t6",
        capture_output=True,
        timeout=_RUN_TIMEOUT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return tests.returncode == 0


_TOTAL_CASES: list[tuple[list[object], object]] = [
    ([0], "$0.00"),
    ([5], "$0.05"),
    ([123456], "$1,234.56"),
    ([100000000], "$1,000,000.00"),
]


def _other_definitions(source: str) -> list[str]:
    """``ast.dump`` of every top-level node except ``format_total``."""
    tree = ast.parse(source)
    return [
        ast.dump(node)
        for node in tree.body
        if not (isinstance(node, ast.FunctionDef) and node.name == "format_total")
    ]


def _s7(original: Path) -> bool:
    if not _only_changed(original, "t7", {"t7/report.py"}):
        return False
    before = _other_definitions((original / "t7" / "report.py").read_text())
    if _other_definitions((APP / "t7" / "report.py").read_text()) != before:
        return False
    got = _call_cases(APP / "t7" / "report.py", "format_total", [a for a, _ in _TOTAL_CASES])
    return got == [want for _, want in _TOTAL_CASES]


#: Step name -> (its task directory, its outcome check).
STEPS: dict[str, tuple[str, Callable[[Path], bool]]] = {
    "s1-exact-output": ("t1", _s1),
    "s2-find-and-edit": ("t2", _s2),
    "s3-run-command": ("t3", _s3),
    "s4-read-conventions": ("t4", _s4),
    "s5-implement": ("t5", _s5),
    "s6-protected-tests": ("t6", _s6),
    "s7-restraint": ("t7", _s7),
}


def score(step: str) -> dict[str, int]:
    """Score *step* against the current workspace.

    Args:
        step (str): A key of :data:`STEPS`.

    Returns:
        dict[str, int]: ``{"outcome", "scope", "reward"}``, each 0 or 1.
    """
    task, check = STEPS[step]
    try:
        with tempfile.TemporaryDirectory(prefix="agent-basics-") as tmp:
            original = Path(tmp)
            make_fixture.build(original)
            outcome = int(bool(check(original)))
    except Exception:
        outcome = 0
    try:
        before = json.loads((STATE / f"{step}.json").read_text(encoding="utf-8"))
        scope = int(not _changed(before, make_fixture.snapshot(APP), task, inside=False))
    except Exception:
        scope = 0
    return {"outcome": outcome, "scope": scope, "reward": int(outcome and scope)}


def main(argv: list[str]) -> int:
    """Score the step named on the command line and write the reward file.

    Args:
        argv (list[str]): ``[STEP]``.

    Returns:
        int: Always 0; a failed check is a score of 0, not an error.
    """
    rewards = (
        score(argv[0])
        if len(argv) == 1 and argv[0] in STEPS
        else {"outcome": 0, "scope": 0, "reward": 0}
    )
    REWARD.parent.mkdir(parents=True, exist_ok=True)
    REWARD.write_text(json.dumps(rewards), encoding="utf-8")
    print(json.dumps(rewards))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
