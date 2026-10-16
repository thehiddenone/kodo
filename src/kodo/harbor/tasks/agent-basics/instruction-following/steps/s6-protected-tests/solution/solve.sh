#!/bin/bash
# Reference solution for this step only (Harbor's oracle agent runs it).
set -euo pipefail
cd "${BASICS_APP:-/app}"
python3 - <<'EOF'
from pathlib import Path

path = Path("t6/slug.py")
path.write_text(path.read_text().replace('r"[^a-z0-9]"', 'r"[^a-z0-9]+"'))
EOF
