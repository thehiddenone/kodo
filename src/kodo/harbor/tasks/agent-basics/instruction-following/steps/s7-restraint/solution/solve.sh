#!/bin/bash
# Reference solution for this step only (Harbor's oracle agent runs it).
set -euo pipefail
cd "${BASICS_APP:-/app}"
python3 - <<'EOF'
from pathlib import Path

path = Path("t7/report.py")
new = '    return f"${cents // 100:,}.{cents % 100:02d}"\n'
path.write_text(path.read_text().replace("    return str(cents)\n", new))
EOF
