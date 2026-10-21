#!/usr/bin/env bash
# Separate runtime/version and go.mod checks; never permit toolchain downloads.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
expected="$(awk '$1 == "go" { print $2 }' "$script_dir/../tools/tool-locks.txt" | sort -u)"
if [[ ! "$expected" =~ ^go[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo '::error title=GF13::Expected one consistent, stable Go version in the lock' >&2
  exit 1
fi
if [ "${GOTOOLCHAIN:-}" != local ]; then
  echo '::error title=GF13::GOTOOLCHAIN must be local; automatic toolchain downloads are forbidden' >&2
  exit 1
fi
case "${1:-}" in
  version)
    actual="$(go version)"
    echo "$actual"
    if [ "$(awk '{print $3}' <<< "$actual")" != "$expected" ]; then
      echo "::error title=GF13::Expected locked Go $expected, got $actual" >&2
      exit 1
    fi
    ;;
  module)
    shift
    if [ "$#" -eq 0 ]; then
      echo 'Usage: verify-locked-go.sh module <go.mod> [...]' >&2
      exit 1
    fi
    for module in "$@"; do
      required="$(awk '$1 == "go" { print $2; count++ } END { if (count != 1) exit 1 }' "$module")" || {
        echo "::error title=GF13::Cannot read exactly one go directive from $module" >&2
        exit 1
      }
      if [[ ! "$required" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
        echo "::error title=GF13::Invalid go directive in $module: $required" >&2
        exit 1
      fi
      if ! awk -v required="$required" -v locked="${expected#go}" 'BEGIN {
        split(required, r, "."); split(locked, l, ".");
        for (i = 1; i <= 3; i++) {
          if (r[i] + 0 < l[i] + 0) exit 0;
          if (r[i] + 0 > l[i] + 0) exit 1;
        }
        exit 0;
      }'; then
        echo "::error title=GF13::$module requires Go $required, newer than locked $expected; update the reviewed lock instead of downloading a toolchain" >&2
        exit 1
      fi
    done
    ;;
  *) echo 'Usage: verify-locked-go.sh version | module <go.mod> [...]' >&2; exit 1 ;;
esac
