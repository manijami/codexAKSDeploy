#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ "$#" -ne 0 ]; then echo 'This launcher executes only the sealed 107-case pending continuation; no extra arguments accepted.'; exit 2; fi
if [ "$(uname -s)" != Darwin ] || [ ! -x /usr/bin/caffeinate ]; then echo 'Use ordinary Mac Terminal with caffeinate available.'; exit 2; fi
mkdir -p reports/full_pack_continuation_logs
log_path="reports/full_pack_continuation_logs/20261115T115541332471Z_$(date -u +%Y%m%dT%H%M%SZ)_$$.log"
printf 'Linked pending continuation: 107 cases only. Progress log: %s\n' "$log_path"
/usr/bin/caffeinate -i .venv/bin/python scripts/continue_campaign.py \
  --campaign-id 20261115T115541332471Z --execute 2>&1 | tee -a "$log_path"
