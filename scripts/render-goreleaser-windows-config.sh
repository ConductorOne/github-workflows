#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <windows-pre-build-script> <caller-root>" >&2
  exit 2
fi

windows_pre_build_script="$1"
caller_root="$2"
WINDOWS_BEFORE_HOOKS_BLOCK=""
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -n "$windows_pre_build_script" ]; then
  if ! bash "$script_dir/validate-safe-relative-path.sh" "$windows_pre_build_script"; then
    echo "::error::windows_pre_build_script must be a relative path with safe characters and without '..' traversal. Got: $windows_pre_build_script" >&2
    exit 1
  fi
  if [ ! -f "$caller_root/$windows_pre_build_script" ]; then
    echo "::error::Windows pre-build script not found: $windows_pre_build_script" >&2
    exit 1
  fi
  # These quotes are YAML data for envsubst, not a shell command.
  # shellcheck disable=SC2089
  printf -v WINDOWS_BEFORE_HOOKS_BLOCK 'before:\n  hooks:\n    - "bash ./%s"\n' "$windows_pre_build_script"
fi
# Export the literal YAML quoting unchanged for envsubst.
# shellcheck disable=SC2090
export WINDOWS_BEFORE_HOOKS_BLOCK

envsubst < "$script_dir/../templates/.goreleaser-windows-template.yaml.tmpl"
