#!/usr/bin/env bash
# Expectations come only from locked-tools outputs through the environment.
# Never parse a checkout's lock or fall back to positional lock arguments.
set -euo pipefail
fail() { echo "::error title=locked-tools::$*" >&2; exit 1; }
mode="${1:-}"
case "$mode" in version|module) shift;; *) fail 'Usage: verify-locked-go.sh version | module <go.mod> [...]';; esac
[ "${GOTOOLCHAIN:-}" = local ] || fail 'GOTOOLCHAIN must be local; automatic toolchain downloads are forbidden'

expected="${EXPECTED_GO_VERSION:-}"
lock_hash="${EXPECTED_LOCK_SHA256:-}"
lock_path="${LOCK_PATH:-}"
[[ "$expected" =~ ^go[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'EXPECTED_GO_VERSION must be one stable goX.Y.Z version from locked-tools'
[[ "$lock_hash" =~ ^[0-9a-f]{64}$ ]] || fail 'EXPECTED_LOCK_SHA256 must be the locked-tools SHA256 output'
[ -n "$lock_path" ] || fail 'LOCK_PATH must be the absolute locked-tools lock_path output'
if [[ "$lock_path" == *$'\n'* || "$lock_path" == *$'\r'* ]]; then fail 'LOCK_PATH must not contain line breaks'; fi
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) lock_path="$(cygpath -u "$lock_path")";; esac
[[ "$lock_path" = /* ]] || fail 'LOCK_PATH must be absolute'
if [ ! -f "$lock_path" ] || [ ! -r "$lock_path" ]; then fail "LOCK_PATH missing or unreadable: $lock_path"; fi
if command -v sha256sum >/dev/null 2>&1; then
  actual_hash="$(sha256sum "$lock_path" | awk '{print $1}')"
else
  actual_hash="$(shasum -a 256 "$lock_path" | awk '{print $1}')"
fi
[ "$actual_hash" = "$lock_hash" ] || fail 'Lock identity mismatch: LOCK_PATH no longer matches EXPECTED_LOCK_SHA256'
echo "Verified Go lock identity: $lock_hash $lock_path"

check_version() {
  local actual
  actual="$(go version)"
  echo "$actual"
  [ "$(awk '{print $3}' <<< "$actual")" = "$expected" ] || fail "Expected locked Go $expected, got $actual"
}
version_le() {
  awk -v required="$1" -v locked="$2" 'BEGIN {
    split(required, r, "."); split(locked, l, ".");
    for (i = 1; i <= 3; i++) {
      if (r[i] + 0 < l[i] + 0) exit 0;
      if (r[i] + 0 > l[i] + 0) exit 1;
    }
    exit 0;
  }'
}
case "$mode" in
  version)
    [ "$#" -eq 0 ] || fail 'Usage: verify-locked-go.sh version'
    check_version ;;
  module)
    [ "$#" -gt 0 ] || fail 'Usage: verify-locked-go.sh module <go.mod> [...]'
    requirements=()
    for module in "$@"; do
      required="$(awk '{ sub(/\r$/, "") } $1 == "go" { print $2; count++ } END { if (count != 1) exit 1 }' "$module")" || \
        fail "Cannot read exactly one go directive from $module"
      [[ "$required" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || fail "Invalid go directive in $module: $required"
      version_le "$required" "${expected#go}" || \
        fail "$module requires Go $required, newer than locked $expected; update the reviewed lock instead of downloading a toolchain"
      requirements+=("$required")
    done
    check_version
    index=0
    for module in "$@"; do
      listed="$(cd "$(dirname "$module")" && GOTOOLCHAIN=local GOWORK=off GOPROXY=off GOSUMDB=off \
        go list -m -mod=readonly -modfile="$(basename "$module")" -f '{{.GoVersion}}')" || \
        fail "GOTOOLCHAIN=local go list -m failed for $module"
      listed="${listed%$'\r'}"
      [[ "$listed" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || fail "Invalid GoVersion from go list -m for $module: $listed"
      required="${requirements[$index]}"
      if ! version_le "$listed" "$required" || ! version_le "$required" "$listed"; then
        fail "go list -m version disagrees with the go directive in $module"
      fi
      index=$((index+1))
    done ;;
esac
