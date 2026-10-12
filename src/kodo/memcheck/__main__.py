"""Entry point for ``python -m kodo.memcheck`` and the ``kodo-memcheck`` CLI."""

from __future__ import annotations

import sys

from kodo.memcheck import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
