#!/usr/bin/env bash
# Transitional entry point; implementation and lock live with the action.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$script_dir/../.github/actions/locked-tools/verify-locked-go.sh" "$@"
