#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
bash_real=$BASH
mkdir -p "$work/repo/scripts" "$work/bin" "$work/state"
SORT_REAL=$(type -P sort)
export SORT_REAL
# Replace only fixed host lookup boundaries, so missing-Go tests cannot find a
# workstation compiler accidentally. PATH/toolcache inputs remain real inputs.
sed -e "s@/opt/hostedtoolcache@$work/hosted-cache@g" \
    -e "s@/Users/runner/hostedtoolcache@$work/mac-cache@g" \
    -e "s@/c/hostedtoolcache/windows@$work/windows-cache@g" \
    -e "s@/opt/homebrew/bin/go@$work/homebrew/go@g" \
    -e "s@/usr/local/go/bin/go@$work/local/go@g" \
    "$root/scripts/ca-trust-guard.sh" > "$work/repo/scripts/ca-trust-guard.sh"
for utility in dirname mkdir rm sort uname cat chmod sed; do
  ln -s "$(type -P "$utility")" "$work/bin/$utility"
done
ln -s "$bash_real" "$work/bin/bash"
make_go() {
  local destination=$1
  mkdir -p "$(dirname "$destination")"
  # The generated stub must evaluate $1 when invoked, not while writing it.
  # shellcheck disable=SC2016
  printf '#!/bin/bash\nif [ "$1" = version ]; then exit %s; fi\n' "${2:-0}" > "$destination"
  cat >> "$destination" <<'GO'
set -euo pipefail
[ "$1" = build ] && [ "$2" = -o ]
[ "$GOTOOLCHAIN" = local ] && [ "$GO111MODULE" = off ]
[ "$GOPROXY" = off ] && [ "$GOSUMDB" = off ] && [ "$GOWORK" = off ]
printf '%s\n' "$0" > "$RUNNER_TEMP/compiler-used"
cat > "$3" <<'GUARD'
#!/bin/bash
set -euo pipefail
if [ "$1" = record ]; then printf '%s\n' "$*" >> "$RUNNER_TEMP/recorded-outcomes"; fi
exit 0
GUARD
chmod +x "$3"
GO
  chmod +x "$destination"
}
run_guard() {
  PATH="$work/bin" RUNNER_TEMP="$work/state" RUNNER_TOOLCACHE="$work/toolcache with spaces" \
    RUNNER_TOOL_CACHE="$work/standard-cache" GUARD_MODE=enforce GUARD_APPLY_DISTRUST=true \
    GUARD_OS_DENY=true GUARD_OS_DENY_ELEVATION=fail \
    "$bash_real" "$work/repo/scripts/ca-trust-guard.sh" "$@" > "$work/stdout" 2> "$work/stderr"
}
assert_compiler() {
  run_guard probe before
  [ "$(cat "$work/state/compiler-used")" = "$1" ] || {
    echo "unexpected compiler: $(cat "$work/state/compiler-used") (wanted $1)" >&2; exit 1;
  }
}
make_go "$work/bin/go"
make_go "$work/toolcache with spaces/go/1.99.0/arm64/bin/go"
assert_compiler "$work/bin/go"
rm "$work/bin/go"
make_go "$work/toolcache with spaces/go/1.99.0/arm64/bin/go" 126
make_go "$work/toolcache with spaces/go/1.9.12/x64/bin/go"
make_go "$work/toolcache with spaces/go/1.27.2/x64/bin/go"
make_go "$work/toolcache with spaces/go/1.27.12/arm64/bin/go"
assert_compiler "$work/toolcache with spaces/go/1.27.12/arm64/bin/go"
make_go "$work/standard-cache/go/1.28.0/x64/bin/go.exe"
assert_compiler "$work/standard-cache/go/1.28.0/x64/bin/go.exe"
rm -rf "$work/toolcache with spaces" "$work/standard-cache"
make_go "$work/mac-cache/go/1.27.3/arm64/bin/go"
make_go "$work/hosted-cache/go/1.27.2/x64/bin/go"
# Native-tool CRLF output must not turn a selected executable path into go\r.
rm "$work/bin/sort"
cat > "$work/bin/sort" <<'SORT'
#!/bin/bash
set -euo pipefail
"$SORT_REAL" "$@" | sed 's/$/\r/'
SORT
chmod +x "$work/bin/sort"
assert_compiler "$work/mac-cache/go/1.27.3/arm64/bin/go"
grep -q $'\r' "$work/state/ca-trust-guard/go-candidates-sorted.tsv"
rm -rf "$work/mac-cache" "$work/hosted-cache"
make_go "$work/homebrew/go"
make_go "$work/local/go"
assert_compiler "$work/homebrew/go"
rm "$work/homebrew/go"
assert_compiler "$work/local/go"
rm "$work/local/go"
if run_guard probe before; then echo 'missing compiler did not fail closed' >&2; exit 1; fi
grep -q '::error.*Go compiler not found' "$work/stderr"
grep -Fq "$work/mac-cache" "$work/stderr"
grep -Fq "$work/homebrew/go" "$work/stderr"
grep -Fq "$work/local/go" "$work/stderr"
# A failed OS deny must retain its ERROR evidence AND make the step itself red.
make_go "$work/bin/go"
run_guard probe before
printf '#!/bin/bash\nexit 7\n' > "$work/repo/scripts/deny-os.sh"
rc=0
run_guard deny-os || rc=$?
[ "$rc" = 1 ]
grep -q -- '--verdict ERROR' "$work/state/recorded-outcomes"
printf 'ca-trust-guard bootstrap tests passed (PATH/cache/fallback ordering, highest runnable version, no-Go failure, red deny step)\n'
