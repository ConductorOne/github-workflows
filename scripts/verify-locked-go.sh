#!/usr/bin/env bash
# DEPRECATED compatibility entry point; remove after workflow gates use outputs/env.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${EXPECTED_GO_VERSION:-}${EXPECTED_LOCK_SHA256:-}${LOCK_PATH:-}" ] && [ "$#" -gt 0 ]; then
  mode="$1"; shift
  lock="${TOOL_LOCK_FILE:-$script_dir/../.github/actions/locked-tools/tool-locks.txt}"
  exec bash "$script_dir/../.github/actions/locked-tools/verify-locked-go.sh" "$mode" "$lock" "$@"
fi
exec bash "$script_dir/../.github/actions/locked-tools/verify-locked-go.sh" "$@"
