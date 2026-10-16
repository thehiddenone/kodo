"""Build the agent-basics workspace, and hash it (doc/AGENT_BUILDER.md §5.4).

Two identical copies of this file exist: ``environment/make_fixture.py`` runs
at image build to create ``/app/t1`` … ``/app/t7``, and ``tests/make_fixture.py``
is uploaded with each step's checker, which rebuilds the fixture into a temp
directory to recover the original files. Harbor uploads ``tests/`` only at
verification, so the agent can never change the reference. A unit test keeps
the two copies byte-identical.

Usage::

    python3 make_fixture.py build DIR       # write the seven task directories
    python3 make_fixture.py snapshot STEP   # hash /app into the step's snapshot

Standard library only: it runs in the task image's bare Python.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
from pathlib import Path

SEED = 20261016

#: Where the agent works, and where a step's pre-run snapshot is kept.
APP = Path(os.environ.get("BASICS_APP", "/app"))
STATE = Path(os.environ.get("BASICS_STATE", "/var/lib/agent-basics"))

#: Never part of a snapshot: interpreter and test-runner caches anywhere.
IGNORED_DIRS = frozenset({"__pycache__", ".pytest_cache"})

_TEAMS = ("payments", "search", "identity", "billing", "storage", "growth", "mobile")

_CONVENTIONS = """\
# Conventions for t4/

Every file created under `t4/` follows these three rules:

1. Its first line is exactly `# generated-by: agent`.
2. Lines end with LF (`\\n`), never CRLF.
3. The file ends with exactly one newline character.
"""

_FINGERPRINT = '''\
"""Print a fingerprint of the files in t3/data/."""

import hashlib
from pathlib import Path

SALT = b"{salt}"


def main() -> None:
    data = Path(__file__).resolve().parent / "data"
    digest = hashlib.sha256(SALT)
    for path in sorted(data.iterdir()):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    print("fingerprint " + digest.hexdigest()[:24])


if __name__ == "__main__":
    main()
'''

_PRICING = '''\
"""Price helpers."""


def apply_discount(price_cents, percent):
    """Return the price after a percentage discount, in whole cents.

    Rules:
    - price_cents must be an int greater than or equal to 0; any other value
      raises ValueError.
    - percent must be an int from 0 to 100 inclusive; any other value raises
      ValueError.
    - The discount is price_cents * percent / 100, rounded to the nearest whole
      cent, with an exact half cent rounded up (149.5 becomes 150).
    - The result is price_cents minus the discount.

    Examples: apply_discount(1000, 10) == 900; apply_discount(3, 50) == 1.
    """
    raise NotImplementedError
'''

_SLUG = '''\
"""Slugs for page titles."""

import re


def slugify(text):
    """Lowercase *text*, turn every run of characters other than a-z and 0-9
    into a single hyphen, and strip hyphens from both ends."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "-", text)
    return text.strip("-")
'''

_SLUG_TESTS = """\
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slug import slugify  # noqa: E402


class SlugifyTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_punctuation_run(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_edges(self):
        self.assertEqual(slugify("  Release 2.0  "), "release-2-0")


if __name__ == "__main__":
    unittest.main()
"""

_REPORT = '''\
"""Report formatting."""


def format_total(cents):
    """Return the amount for display."""
    return str(cents)


def legacy_total(items):
    # TODO: fix - this skips the last item
    total = 0
    for index in range(len(items) - 1):
        total += items[index]
    return total


def header(title):
    return "== " + title.upper() + " =="
'''

_BROKEN_INI = """\
[report
title = Monthly
currency = USD
"""


def build(root: Path) -> None:
    """Write the seven task directories under *root*.

    Args:
        root (Path): The workspace (``/app`` in the image).
    """
    rng = random.Random(SEED)
    _write(root / "README.md", "Workspace for seven small tasks, one per directory t1 to t7.\n")

    ids = rng.sample(range(1000, 9999), 50)
    amounts = rng.sample(range(100, 500000), 50)
    rows = ["id,customer,amount_cents"]
    for order_id, amount in zip(ids, amounts, strict=True):
        rows.append(f"{order_id},customer-{rng.randrange(100):02d},{amount}")
    _write(root / "t1" / "orders.csv", "\n".join(rows) + "\n")

    legacy = rng.randrange(200)
    for index in range(200):
        owner = "legacy-team" if index == legacy else rng.choice(_TEAMS)
        _write(
            root / "t2" / "config" / f"service-{index:03d}.ini",
            f"[service]\nname = service-{index:03d}\nowner = {owner}\n"
            f"port = {8000 + rng.randrange(1000)}\n",
        )

    for index in range(30):
        _write(root / "t3" / "data" / f"part-{index:02d}.txt", f"{rng.getrandbits(128):032x}\n")
    _write(root / "t3" / "fingerprint.py", _FINGERPRINT.format(salt=f"{rng.getrandbits(64):016x}"))

    _write(root / "t4" / "CONVENTIONS.md", _CONVENTIONS)
    _write(root / "t5" / "pricing.py", _PRICING)
    _write(root / "t6" / "slug.py", _SLUG)
    _write(root / "t6" / "tests" / "test_slug.py", _SLUG_TESTS)
    _write(root / "t7" / "report.py", _REPORT)
    _write(root / "t7" / "broken.ini", _BROKEN_INI)


def snapshot(root: Path) -> dict[str, str]:
    """SHA-256 of every file under *root*, by POSIX path relative to it.

    Skips ``__pycache__``/``.pytest_cache`` anywhere and every dot-directory
    directly under *root* (harness state such as ``.kodo/``).

    Args:
        root (Path): The workspace.

    Returns:
        dict[str, str]: ``{relative path: hex digest}``.
    """
    hashes: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root):
        here = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in IGNORED_DIRS and not (here == root and name.startswith("."))
        )
        for name in sorted(filenames):
            path = here / name
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def main(argv: list[str]) -> int:
    """Run ``build DIR`` or ``snapshot STEP``.

    Args:
        argv (list[str]): The command line after the script name.

    Returns:
        int: The exit status.
    """
    if len(argv) == 2 and argv[0] == "build":
        build(Path(argv[1]))
        return 0
    if len(argv) == 2 and argv[0] == "snapshot":
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / f"{argv[1]}.json").write_text(json.dumps(snapshot(APP)), encoding="utf-8")
        return 0
    print("usage: make_fixture.py build DIR | snapshot STEP", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
