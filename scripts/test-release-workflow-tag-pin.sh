#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workflow="${script_dir}/../.github/workflows/release.yaml"
expected='${{ inputs.tag }}'

assert_tag_pin() {
  local job="$1"
  local step="$2"
  local got
  got="$(yq -r ".jobs.\"${job}\".steps[] | select(.name == \"${step}\").env.GORELEASER_CURRENT_TAG" "$workflow")"
  if [ "$got" != "$expected" ]; then
    echo "${job}/${step}: GORELEASER_CURRENT_TAG = ${got@Q}, want ${expected@Q}" >&2
    exit 1
  fi
}

assert_tag_pin goreleaser-binaries "Run GoReleaser"
assert_tag_pin goreleaser-windows "Run GoReleaser for Windows"
assert_tag_pin goreleaser-docker "Run GoReleaser for Docker OCI"
assert_tag_pin goreleaser-docker "Run GoReleaser for Lambda"
