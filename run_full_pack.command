#!/bin/bash
# Ordinary macOS Terminal launcher. Never source .env or print private settings.
set -euo pipefail
cd -- "$(dirname -- "$0")"
mode=execute
campaign_path=""
case "${1:-}" in
  --plan) mode=plan; shift ;;
  --preview) mode=preview; shift ;;
  --resume) mode=resume; shift; campaign_path="${1:?Supply the campaign directory}"; shift ;;
  "") ;;
  *) echo 'Usage: ./run_full_pack.command [--plan | --preview | --resume CAMPAIGN_DIR]'; exit 2 ;;
esac
if [ "$#" -ne 0 ]; then echo 'Unexpected launcher arguments.'; exit 2; fi
if [ ! -x .venv/bin/python ]; then echo 'Missing .venv/bin/python; run project setup first.'; exit 2; fi
if [ "$mode" = execute ] || [ "$mode" = resume ]; then
  for framework in ragas deepeval; do
    if [ ! -x ".venv-$framework/bin/python" ]; then echo "Install .venv-$framework using docs/FULL_PACK.md first."; exit 2; fi
  done
  # Each native smoke check uses only synthetic input and a local HTTP mock.
  .venv-ragas/bin/python scripts/check_catalogue_ragas_offline.py
  .venv-deepeval/bin/python scripts/check_deepeval_offline.py
fi
if [ "$mode" = resume ]; then
  if [ ! -f "$campaign_path/manifest.json" ]; then echo 'Resume manifest not found.'; exit 2; fi
  mkdir -p "$campaign_path/logs"
  log_path="$campaign_path/logs/resume_$(date -u +%Y%m%dT%H%M%SZ)_$$.log"
  command_args=(resume --campaign "$campaign_path" --allow-draft-references)
else
  stamp="$(date -u +%Y%m%dT%H%M%SZ)_$$"
  campaign_path="reports/full_pack/$stamp"
  mkdir -p reports/full_pack_logs
  log_path="reports/full_pack_logs/$stamp.log"
  command_args=("$mode" --out "$campaign_path")
  if [ "$mode" = execute ]; then command_args+=(--allow-draft-references); fi
fi
printf 'Campaign: %s\nProgress log: %s\nHTML report: %s/report.html\n' "$campaign_path" "$log_path" "$campaign_path"
if [ "$(uname -s)" = Darwin ] && [ -x /usr/bin/caffeinate ]; then
  /usr/bin/caffeinate -i .venv/bin/python scripts/run_full_pack.py "${command_args[@]}" 2>&1 | tee -a "$log_path"
else
  if [ "$mode" = execute ] || [ "$mode" = resume ]; then echo 'Live launcher requires macOS caffeinate.'; exit 2; fi
  .venv/bin/python scripts/run_full_pack.py "${command_args[@]}" 2>&1 | tee -a "$log_path"
fi
