#!/bin/bash
# Score this step. check.py always writes the reward file; the fallback covers
# an interpreter that never got that far.
mkdir -p /logs/verifier
python3 /tests/check.py s3-run-command \
  || echo '{"outcome": 0, "scope": 0, "reward": 0}' > /logs/verifier/reward.json
