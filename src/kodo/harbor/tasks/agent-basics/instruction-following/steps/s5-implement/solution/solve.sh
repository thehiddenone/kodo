#!/bin/bash
# Reference solution for this step only (Harbor's oracle agent runs it).
set -euo pipefail
cd "${BASICS_APP:-/app}"
python3 - <<'EOF'
from pathlib import Path

path = Path("t5/pricing.py")
body = """\
    if type(price_cents) is not int or price_cents < 0:
        raise ValueError("price_cents must be an int >= 0")
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError("percent must be an int from 0 to 100")
    return price_cents - (price_cents * percent + 50) // 100
"""
path.write_text(path.read_text().replace("    raise NotImplementedError\n", body))
EOF
