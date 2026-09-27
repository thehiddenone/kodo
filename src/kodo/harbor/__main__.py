"""Entry point for ``python -m kodo.harbor`` and the ``kodo-harbor`` CLI."""

from __future__ import annotations

import sys

from kodo.harbor import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
