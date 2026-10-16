#!/bin/bash
# Reference solution for this step only (Harbor's oracle agent runs it).
set -euo pipefail
cd "${BASICS_APP:-/app}"
python3 t3/fingerprint.py | head -n 1 > t3/fingerprint.txt
