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
find_runner_go() {
  local go_path cache arch binary candidate version_dir version major minor patch rest
  local probe_locations="PATH"
  go_path=$(command -v go 2>/dev/null || true)
  go_path=${go_path%$'\r'}
  if [ -n "$go_path" ] && [ -x "$go_path" ]; then printf '%s\n' "$go_path"; return 0; fi

  : > "$CA_GUARD_STATE/go-candidates.tsv"
  for cache in "${RUNNER_TOOLCACHE:-}" "${RUNNER_TOOL_CACHE:-}" \
    /opt/hostedtoolcache /Users/runner/hostedtoolcache /c/hostedtoolcache/windows; do
    cache=${cache%$'\r'}
    [ -n "$cache" ] || continue
    probe_locations="$probe_locations; $cache/go/*/{arm64,x64}/bin/go[.exe]"
    for arch in arm64 x64; do
      for binary in go go.exe; do
        for candidate in "$cache"/go/*/"$arch"/bin/"$binary"; do
          [ -x "$candidate" ] || continue
          version_dir=${candidate%/bin/*}; version_dir=${version_dir%/"$arch"}
          version=${version_dir##*/}
          major=${version%%.*}; rest=${version#*.}; minor=${rest%%.*}; patch=${rest#*.}
          case "$major:$minor:$patch" in *[!0-9:]*|:*|*::*|*:) continue;; esac
          printf '%s\t%s\t%s\t%s\n' "$major" "$minor" "$patch" "$candidate" \
            >> "$CA_GUARD_STATE/go-candidates.tsv"
        done
      done
    done
  done
  sort -t "$(printf '\t')" -k1,1nr -k2,2nr -k3,3nr "$CA_GUARD_STATE/go-candidates.tsv" \
    > "$CA_GUARD_STATE/go-candidates-sorted.tsv"
  while IFS="$(printf '\t')" read -r major minor patch candidate; do
    candidate=${candidate%$'\r'}
    # A cache may include another architecture. Pick the highest runnable
    # compiler without installing an emulator or downloading anything.
    if (cd "$CA_GUARD_STATE" && GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
      "$candidate" version >/dev/null 2>&1); then
      printf '%s\n' "$candidate"; return 0
    fi
  done < "$CA_GUARD_STATE/go-candidates-sorted.tsv"
  for candidate in /opt/homebrew/bin/go /usr/local/go/bin/go; do
    probe_locations="$probe_locations; $candidate"
    if [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return 0; fi
  done
  printf '::error title=CA trust guard::Go compiler not found; probed %s\n' "$probe_locations" >&2
  return 1
}
if [ "${1:-}" = probe ] && [ "${2:-}" = before ]; then
  rm -f "$CA_GUARD_STATE/before.jsonl" "$CA_GUARD_STATE/after.jsonl" "$CA_GUARD_STATE/os-deny.jsonl" \
    "$CA_GUARD_STATE/apply-clean-bundle.jsonl" "$CA_GUARD_STATE/summary.jsonl" "$CA_GUARD_BIN"
  go_compiler=$(find_runner_go)
  printf 'CA trust guard bootstrap compiler: %s\n' "$go_compiler" >&2
  (cd "$root" && GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOWORK=off GOFLAGS= GO111MODULE=off \
    "$go_compiler" build -o "$CA_GUARD_BIN" ./cmd/ca-trust-guard)
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
      exit 1
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
