#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ "$#" -ne 0 ]; then echo 'Fixed linked safety continuation: no extra arguments accepted.'; exit 2; fi
if [ "$(uname -s)" != Darwin ]; then echo 'Use ordinary Mac Terminal.'; exit 2; fi
mkdir -p reports/safety_guardrails_logs
log_path="reports/safety_guardrails_logs/v2_$(date -u +%Y%m%dT%H%M%SZ)_$$.log"
/usr/bin/caffeinate -i .venv/bin/python scripts/continue_safety_guardrails.py --execute 2>&1 | tee -a "$log_path"
