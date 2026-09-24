"""Entry point for ``python -m kodo.headless`` and the ``kodo-headless`` CLI."""

from __future__ import annotations

import sys

from kodo.headless import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
