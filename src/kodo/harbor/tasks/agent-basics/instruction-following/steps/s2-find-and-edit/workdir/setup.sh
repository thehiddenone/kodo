#!/bin/bash
# Snapshot the workspace before the agent runs, for the checker's `scope` key
# (doc/AGENT_BUILDER.md §5.4.3). Harbor uploaded this script into the working
# directory, where the agent would see it, so it deletes itself first.
set -euo pipefail
rm -f -- "$0"
python3 "${BASICS_FIXTURE:-/opt/agent-basics/make_fixture.py}" snapshot s2-find-and-edit
