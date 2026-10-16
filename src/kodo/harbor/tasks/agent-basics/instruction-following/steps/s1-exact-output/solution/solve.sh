#!/bin/bash
# Reference solution for this step only (Harbor's oracle agent runs it).
set -euo pipefail
cd "${BASICS_APP:-/app}"
python3 - <<'EOF'
import csv

with open("t1/orders.csv", newline="") as handle:
    rows = list(csv.DictReader(handle))
top = max(rows, key=lambda row: int(row["amount_cents"]))
with open("t1/summary.txt", "w", newline="") as handle:
    handle.write(f"rows={len(rows)}\nmax_id={top['id']}\n")
EOF
