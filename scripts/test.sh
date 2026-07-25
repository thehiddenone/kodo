#!/usr/bin/env bash
set -euo pipefail

# Resolve project root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Optional: single test or suite selector passed as first argument
# Examples:
#   ./scripts/test.sh                          # run all tests
#   ./scripts/test.sh test/test_orders.py       # run a test file
#   ./scripts/test.sh test/test_orders.py::test_refund  # run a single test
#   ./scripts/test.sh -k refund                # pytest -k filter
SELECTOR="${1:-}"

if [ -n "$SELECTOR" ]; then
    echo "Running test(s) matching: $SELECTOR"
    hatch run test "$SELECTOR"
else
    echo "Running full test suite..."
    hatch run test
fi
