#!/bin/bash
# Reference solution for this step only (Harbor's oracle agent runs it).
set -euo pipefail
cd "${BASICS_APP:-/app}"
mkdir -p t4/notes
printf '# generated-by: agent\nhello\n' > t4/notes/hello.txt
