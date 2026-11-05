#!/usr/bin/env bash
set -euo pipefail
args=()
if [ "${MONITOR_BOOTSTRAP:-false}" = true ]; then
  args+=(--bootstrap)
fi
python3 scripts/rekor_digest_check.py --entry-helper "${REKOR_ENTRY_HELPER:?missing proof helper}" "${args[@]}"
