#!/usr/bin/env bash
set -euo pipefail

# Resolve project root (directory containing this script's parent)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "Building distribution (wheel + sdist)..."
hatch build
