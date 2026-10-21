#!/usr/bin/env bash
# Glue only: build the stdlib command with the runner-image toolchain, then
# delegate observations/decisions to Go and OS mutations to their own scripts.
# Only this bootstrap build is module-off for runner-Go compatibility (Go 1.22+
# language/API floor, stdlib only, still local/offline). Vet/tests retain the
# repository module and its Go 1.27.3 toolchain requirement.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fixtures="$root/.github/actions/ca-trust-guard/fixtures"
mode=${GUARD_MODE:-enforce}
apply=${GUARD_APPLY_DISTRUST:-true}
deny=${GUARD_OS_DENY:-true}
elevation=${GUARD_OS_DENY_ELEVATION:-fail}
case "$mode" in report|enforce) ;; *) echo 'mode must be report or enforce' >&2; exit 2;; esac
case "$apply:$deny" in true:true|true:false|false:true|false:false) ;;
  *) echo 'apply-distrust and os-deny must be true or false' >&2; exit 2;;
esac
case "$elevation" in fail|warn) ;; *) echo 'os-deny-elevation must be fail or warn' >&2; exit 2;; esac
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) CA_GUARD_MSYS_ROOT=$(cygpath -w /); export CA_GUARD_MSYS_ROOT ;;
esac
export CA_GUARD_STATE="${RUNNER_TEMP:?}/ca-trust-guard"
export CA_GUARD_BIN="$CA_GUARD_STATE/guard.exe"
mkdir -p "$CA_GUARD_STATE"
if [ "${1:-}" = probe ] && [ "${2:-}" = before ]; then
  rm -f "$CA_GUARD_STATE/before.jsonl" "$CA_GUARD_STATE/after.jsonl" "$CA_GUARD_STATE/os-deny.jsonl" \
    "$CA_GUARD_STATE/apply-clean-bundle.jsonl" "$CA_GUARD_STATE/summary.jsonl" "$CA_GUARD_BIN"
  (cd "$root" && GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOWORK=off GOFLAGS= GO111MODULE=off \
    go build -o "$CA_GUARD_BIN" ./cmd/ca-trust-guard)
fi
case "${1:-}" in
  probe)
    "$CA_GUARD_BIN" probe --phase "${2:?}" --stores auto --chains "$fixtures/chains.json" \
      --out "$CA_GUARD_STATE/${2}.jsonl" --apply-distrust="$apply" ;;
  deny-os)
    if bash "$root/scripts/deny-os.sh"; then
      if [ ! -s "$CA_GUARD_STATE/os-deny.jsonl" ]; then
        "$CA_GUARD_BIN" record --out "$CA_GUARD_STATE/os-deny.jsonl" --verdict NO-CHANGES --detail 'no roots changed'
      fi
    else
      "$CA_GUARD_BIN" record --out "$CA_GUARD_STATE/os-deny.jsonl" --verdict ERROR \
        --detail 'OS deny refused a mismatch or failed mutation/verification; see preceding diagnostics'
    fi ;;
  apply-clean-bundle)
    verdict=APPLIED
    if ! bash "$root/scripts/apply-clean-bundle.sh"; then verdict=ERROR; fi
    "$CA_GUARD_BIN" record --out "$CA_GUARD_STATE/apply-clean-bundle.jsonl" \
      --phase apply-clean-bundle --store clean-bundle-env --verdict "$verdict" \
      --detail 'clean-bundle environment leg; native Windows only redirects Git/curl' ;;
  summarize)
    "$CA_GUARD_BIN" summarize --mode "$mode" --apply-distrust="$apply" --os-deny="$deny" \
      --os-deny-elevation="$elevation" --in "$CA_GUARD_STATE" ;;
  *) echo 'expected probe before|after, deny-os, apply-clean-bundle, or summarize' >&2; exit 2 ;;
esac
