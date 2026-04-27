"""Entry point for ``python -m kodo.server``."""

from __future__ import annotations

import argparse
import sys
import traceback

from aiohttp import web

from ._app import DEFAULT_DECISION_TIMEOUT, DEFAULT_MAX_WORKFLOWS, PORT, create_app


def main() -> None:
    """Parse CLI arguments and start the orchestrator server.

    Binds to ``127.0.0.1:8042``.
    """
    parser = argparse.ArgumentParser(description="Kōdo orchestrator server")
    parser.add_argument("--max-workflows", type=int, default=DEFAULT_MAX_WORKFLOWS)
    parser.add_argument("--decision-timeout", type=float, default=DEFAULT_DECISION_TIMEOUT)
    args = parser.parse_args()

    app = create_app(
        max_workflows=args.max_workflows,
        decision_timeout=args.decision_timeout,
    )
    try:
        web.run_app(app, host="127.0.0.1", port=PORT)
    except:
        _, exc, _ = sys.exc_info()
        if isinstance(exc, RuntimeError) and str(exc) == 'Event loop stopped before Future completed.':
            pass
        else:
            sys.stderr.write(traceback.format_exc())


if __name__ == "__main__":
    main()
