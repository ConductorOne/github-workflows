#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="${script_dir}/normalize-release-options.sh"

assert_output() {
  local want="$1"
  shift
  local got
  got="$(bash "$script" "$@")"
  if [ "$got" != "$want" ]; then
    echo "got:  $got" >&2
    echo "want: $want" >&2
    exit 1
  fi
}

assert_failure() {
  if bash "$script" "$@" >/dev/null 2>&1; then
    echo "expected failure: $script $*" >&2
    exit 1
  fi
}

assert_output $'go_main_package=./cmd/bridge-client\nbrew_tap=homebrew-baton' bridge-client "" ""
assert_output $'go_main_package=./\nbrew_tap=homebrew-cone' c1i ./ homebrew-cone
assert_output $'go_main_package=./cmd/release\nbrew_tap=homebrew-baton' c1i ./cmd/release homebrew-baton

assert_failure c1i /cmd/c1i homebrew-baton
assert_failure c1i ../cmd/c1i homebrew-baton
assert_failure c1i ./cmd/../c1i homebrew-baton
assert_failure c1i ./cmd//c1i homebrew-baton
assert_failure c1i ./cmd/ homebrew-baton
assert_failure c1i ./ owner/homebrew-cone
assert_failure c1i ./ 'homebrew cone'
