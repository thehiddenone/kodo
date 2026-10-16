#!/bin/bash
# Reference solution for this step only (Harbor's oracle agent runs it).
set -euo pipefail
cd "${BASICS_APP:-/app}"
python3 - <<'EOF'
from pathlib import Path

for path in sorted(Path("t2/config").iterdir()):
    text = path.read_bytes()
    if b"owner = legacy-team\n" in text:
        path.write_bytes(text.replace(b"owner = legacy-team\n", b"owner = platform-team\n"))
EOF
