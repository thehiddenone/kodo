#!/usr/bin/env bash
set -euo pipefail

# Resolve project root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== Step 1/4: format ==="
bash "$SCRIPT_DIR/format.sh"

echo "=== Step 2/4: build ==="
bash "$SCRIPT_DIR/build.sh"

echo "=== Step 3/4: static_analysis ==="
bash "$SCRIPT_DIR/static_analysis.sh"

echo "=== Step 4/4: test ==="
bash "$SCRIPT_DIR/test.sh"

echo "=== full_build complete ==="
