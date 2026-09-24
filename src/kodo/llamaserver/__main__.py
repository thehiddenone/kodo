"""Entry point for ``python -m kodo.llamaserver`` and the ``kodo-llama-server`` CLI."""

from __future__ import annotations

import sys

from kodo.llamaserver import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
