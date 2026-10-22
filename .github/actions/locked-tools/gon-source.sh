#!/usr/bin/env bash
# ConductorOne/gon v0.2.10 has no arm64 release asset; build its pinned source.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock="${TOOL_LOCK_FILE:-$script_dir/tool-locks.txt}"
commit="$(awk '$1 == "gon" && $3 == "darwin/*" && $4 == "source" && NF == 4 { print $2; count++ } END { if (count != 1) exit 1 }' "$lock")"
if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo '::error title=GF13::Expected a single immutable gon source commit lock' >&2
  exit 1
fi
case "${1:-}" in
  ref)
    printf 'ref=%s\n' "$commit" >> "${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"
    ;;
  build)
    if [ "$#" -ne 3 ]; then
      echo 'Usage: gon-source.sh build <source-dir> <dest-dir>' >&2
      exit 1
    fi
    source_dir="$2"
    dest="$3"
    if [ "$(git -C "$source_dir" rev-parse HEAD)" != "$commit" ]; then
      echo '::error title=GF13::gon checkout does not match locked source commit' >&2
      exit 1
    fi
    # Require the verified Go binary and checksum-authenticated module downloads.
    if [ "${GOFLAGS:-}" != -mod=readonly ] || [ "${GOPROXY:-}" != https://proxy.golang.org ] || [ "${GOSUMDB:-}" != sum.golang.org ]; then
      echo '::error title=GF13::gon requires readonly modules, proxy.golang.org, and sum.golang.org' >&2
      exit 1
    fi
    bash "$script_dir/verify-locked-go.sh" version
    bash "$script_dir/verify-locked-go.sh" module "$source_dir/go.mod"
    mkdir -p "$dest"
    dest="$(cd "$dest" && pwd)"
    # Match the fork's release build directory (the module root has no Go files).
    (cd "$source_dir/cmd/gon" && go build -trimpath -o "$dest/gon" .)
    shasum -a 256 "$dest/gon"
    printf '%s\n' "$dest" >> "${GITHUB_PATH:?GITHUB_PATH must be set}"
    ;;
  *) echo 'Usage: gon-source.sh ref | build <source-dir> <dest-dir>' >&2; exit 1 ;;
esac
