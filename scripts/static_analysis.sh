#!/usr/bin/env bash
set -euo pipefail

# Resolve project root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "Running lint checks..."
hatch run lint

echo "Running type checks..."
hatch run typecheck
