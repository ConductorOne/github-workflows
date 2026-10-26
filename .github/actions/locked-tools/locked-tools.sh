#!/usr/bin/env bash
# Composite-action glue. Artifact verification remains in the gated installers.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "::error title=locked-tools::$*" >&2; exit 1; }
phase="${1:-}"
case "$phase" in prepare|install|gon|outputs) ;; *) fail 'Unknown locked-tools phase' ;; esac
request="${LOCKED_TOOLS:-}"
if [[ -z "$request" || "$request" == *$'\n'* || "$request" == *$'\r'* ]]; then
  fail 'tools must be a nonempty comma-separated list'
fi
request="${request%"${request##*[![:space:]]}"}"
[[ -n "$request" ]] || fail 'tools must be a nonempty comma-separated list'
[[ "$request" != *, ]] || fail 'Empty tool in tools input'
IFS=',' read -r -a requested <<< "$request"
tools=()
seen=,
needs_go=false
for tool in "${requested[@]}"; do
  tool="${tool#"${tool%%[![:space:]]*}"}"
  tool="${tool%"${tool##*[![:space:]]}"}"
  case "$tool" in
    go|node|syft|goreleaser|goreleaser-pro|minisign|gon) ;;
    *) fail "Unsupported tool: $tool" ;;
  esac
  [[ "$seen" != *",$tool,"* ]] || fail "Duplicate tool: $tool"
  seen+="$tool,"
  tools+=("$tool")
  if [ "$tool" = go ] || [ "$tool" = gon ]; then needs_go=true; fi
done
# Go must precede every source build, even when gon is the only requested tool.
if [ "$needs_go" = true ]; then
  ordered=(go)
  for tool in "${tools[@]}"; do
    if [ "$tool" != go ]; then ordered+=("$tool"); fi
  done
  tools=("${ordered[@]}")
fi
case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  MINGW*|MSYS*|CYGWIN*) os=windows ;;
  *) fail 'Unsupported operating system' ;;
esac
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) fail 'Unsupported architecture' ;;
esac
if [ "$phase" = prepare ]; then
  lock="${LOCKS_INPUT:-${ACTION_DIR:-$script_dir}/tool-locks.txt}"
else
  lock="${LOCK_PATH:-${TOOL_LOCK_FILE:-$script_dir/tool-locks.txt}}"
fi
dest="${LOCKED_TOOLS_DEST:?LOCKED_TOOLS_DEST must be set}"
if [[ "$lock" == *$'\n'* || "$lock" == *$'\r'* || "$dest" == *$'\n'* || "$dest" == *$'\r'* ]]; then
  fail 'Lock and destination paths must not contain line breaks'
fi
if [ "$os" = windows ]; then
  lock="$(cygpath -u "$lock")"
  dest="$(cygpath -u "$dest")"
fi
if [ -z "$lock" ] || [ ! -f "$lock" ] || [ ! -r "$lock" ]; then
  fail "Lock file missing or not readable: $lock"
fi
lock="$(cd "$(dirname "$lock")" && pwd)/$(basename "$lock")"
export TOOL_LOCK_FILE="$lock"
lock_row() {
  local tool="$1" platform="$os/$arch"
  if [ "$tool" = gon ]; then
    [ "$os" = darwin ] || fail 'gon source builds require macOS'
    platform='darwin/*'
  fi
  awk -v tool="$tool" -v platform="$platform" '$1 == tool && $3 == platform { print; count++ } END { if (count != 1) exit 1 }' "$lock" || fail "Expected exactly one $tool lock for $platform"
}
gon_ref=
for tool in "${tools[@]}"; do
  row="$(lock_row "$tool")"
  read -r _ version _ checksum url extra <<< "$row"
  [[ "$version" =~ ^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$ ]] || fail "Invalid locked version for $tool"
  if [ "$tool" = gon ]; then
    [[ "$version" =~ ^[0-9a-f]{40}$ && "$checksum" = source && -z "$url" && -z "$extra" ]] || fail 'Invalid gon source lock'
    gon_ref="$version"
  else
    [[ "$checksum" =~ ^[0-9a-f]{64}$ && -n "$url" && -z "$extra" ]] || fail "Invalid artifact lock for $tool"
  fi
done
if [ "$phase" = prepare ] && [ "$needs_go" = true ]; then
  expected_go_version="$(awk '$1 == "go" { print $2 }' "$lock" | sort -u)"
  [[ "$expected_go_version" =~ ^go[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'Expected one consistent, stable Go version in the lock'
else
  expected_go_version="${EXPECTED_GO_VERSION:-}"
fi
if [ "$phase" != prepare ] && [ "$needs_go" = true ]; then
  [[ "$expected_go_version" =~ ^go[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'EXPECTED_GO_VERSION must come from preparation'
  row="$(lock_row go)"
  read -r _ locked_go_version _ <<< "$row"
  [ "$locked_go_version" = "$expected_go_version" ] || fail 'Prepared Go version does not match the verified lock'
fi
export EXPECTED_GO_VERSION="$expected_go_version"
if command -v sha256sum >/dev/null 2>&1; then
  lock_sha256="$(sha256sum "$lock" | awk '{print $1}')"
else
  lock_sha256="$(shasum -a 256 "$lock" | awk '{print $1}')"
fi
if [ "$phase" != prepare ] && [ "$lock_sha256" != "${EXPECTED_LOCK_SHA256:-}" ]; then
  fail 'Lock file changed after preparation'
fi
lock_path="$lock"
if [ "$os" = windows ]; then lock_path="$(cygpath -w "$lock" | tr -d '\r')"; fi
export LOCK_PATH="$lock_path"
mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
case "$phase" in
  prepare)
    normalized="$(IFS=,; printf '%s' "${tools[*]}")"
    gon_path=
    if [ -n "$gon_ref" ]; then
      source_dir="$(mktemp -d "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE must be set}/.locked-tools-gon.XXXXXX")"
      gon_path="$(basename "$source_dir")"
    fi
    {
      printf 'tools=%s\nlock_file=%s\ndest=%s\nlock_sha256=%s\n' "$normalized" "$lock" "$dest" "$lock_sha256"
      printf 'lock_path=%s\ngo_version=%s\n' "$lock_path" "$expected_go_version"
      printf 'gon_ref=%s\ngon_path=%s\n' "$gon_ref" "$gon_path"
    } >> "${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"
    ;;
  install)
    for tool in "${tools[@]}"; do
      [ "$tool" != gon ] || continue
      target="$dest/$tool"
      if [ "$tool" = go ]; then target="$dest"; fi
      if [ "$os" = windows ]; then
        TOOL_LOCK_FILE="$(cygpath -w "$lock")" pwsh -NoProfile -File "$(cygpath -w "$script_dir/install-tool.ps1")" -Tool "$tool" -DestDir "$(cygpath -w "$target")"
      else
        bash "$script_dir/install-tool.sh" "$tool" "$target"
      fi
      if [ "$tool" = go ]; then
        export GOTOOLCHAIN=local
        export GOROOT="$dest/go"
        if [ "$os" = windows ]; then
          GOROOT="$(cygpath -w "$GOROOT")"
          export GOROOT
        fi
        export PATH="$dest/go/bin:$PATH"
        bash "$script_dir/verify-locked-go.sh" version
        printf 'GOTOOLCHAIN=local\n' >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
      fi
    done
    ;;
  gon)
    [ -n "$gon_ref" ] || fail 'gon was not requested'
    gon_path="${LOCKED_GON_PATH:-}"
    [[ "$gon_path" =~ ^\.locked-tools-gon\.[a-zA-Z0-9]+$ ]] || fail 'Invalid temporary gon checkout path'
    source_dir="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE must be set}/$gon_path"
    bash "$script_dir/gon-source.sh" build "$source_dir" "$dest/gon"
    # The action owns this unique checkout. Do not leave it in caller source trees.
    rm -rf -- "$source_dir"
    ;;
  outputs)
    versions='{'
    separator=
    for tool in "${tools[@]}"; do
      row="$(lock_row "$tool")"
      read -r _ version _ <<< "$row"
      printf '%s=%s\n' "$tool" "$version" >> "${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"
      versions+="${separator}\"${tool}\":\"${version}\""
      separator=,
    done
    versions+='}'
    printf 'versions=%s\nlock_sha256=%s\nlock_path=%s\n' "$versions" "$lock_sha256" "$lock_path" >> "$GITHUB_OUTPUT"
    ;;
esac
