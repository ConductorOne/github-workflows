#!/usr/bin/env bash
# Exercises publish-homebrew-formula.sh against stubbed curl and sleep
# binaries placed ahead of the real ones on PATH. git and jq are real.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

stub_dir="${tmp_dir}/bin"
mkdir -p "$stub_dir"

# curl stub: pops the first line of FAKE_CURL_RESPONSES ("STATUS<TAB>BODY"),
# writes BODY to the --output file, prints STATUS, and appends the request
# method and any JSON data it was given to FAKE_CURL_CALLS.
cat > "${stub_dir}/curl" <<'FAKE_CURL'
#!/usr/bin/env bash
method=GET out="" data=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --request) method="$2"; shift 2 ;;
    --output) out="$2"; shift 2 ;;
    --data-binary) data="$(jq -c . "${2#@}")"; shift 2 ;;
    *) shift ;;
  esac
done
line="$(head -n 1 "$FAKE_CURL_RESPONSES")"
tail -n +2 "$FAKE_CURL_RESPONSES" > "${FAKE_CURL_RESPONSES}.next"
mv "${FAKE_CURL_RESPONSES}.next" "$FAKE_CURL_RESPONSES"
status="${line%%	*}"
body="${line#*	}"
printf '%s' "$body" > "$out"
printf '%s\t%s\n' "$method" "$data" >> "$FAKE_CURL_CALLS"
printf '%s' "$status"
FAKE_CURL

cat > "${stub_dir}/sleep" <<'FAKE_SLEEP'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$FAKE_SLEEP_CALLS"
FAKE_SLEEP
chmod +x "${stub_dir}/curl" "${stub_dir}/sleep"

formula="${tmp_dir}/baton-example.rb"
printf 'class BatonExample < Formula\n  url "https://example.invalid/baton-example.tar.gz"\nend\n' > "$formula"
formula_sha="$(git hash-object "$formula")"

export GITHUB_TOKEN=test-token
export HOMEBREW_TAP_REPO=conductorone/homebrew-test
export FORMULA_FILE="$formula"
export FORMULA_PATH=Formula/baton-example.rb
export COMMIT_MESSAGE="Brew formula update for baton-example version v0.1.0"
export HOMEBREW_PUSH_DELAY=2
export FAKE_CURL_RESPONSES="${tmp_dir}/responses"
export FAKE_CURL_CALLS="${tmp_dir}/calls"
export FAKE_SLEEP_CALLS="${tmp_dir}/sleeps"

failures=0
fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

# run_case NAME EXPECTED_EXIT RESPONSES... — RESPONSES are "STATUS<TAB>BODY".
run_case() {
  local name="$1" expected="$2"
  shift 2
  : > "$FAKE_CURL_CALLS"
  : > "$FAKE_SLEEP_CALLS"
  printf '%s\n' "$@" > "$FAKE_CURL_RESPONSES"
  local status=0
  PATH="${stub_dir}:${PATH}" bash "${ROOT_DIR}/scripts/publish-homebrew-formula.sh" > "${tmp_dir}/stdout" 2> "${tmp_dir}/stderr" || status=$?
  if [[ "$status" -ne "$expected" ]]; then
    fail "$name: exit $status, expected $expected"
    cat "${tmp_dir}/stderr" >&2
  fi
}

methods() { cut -f1 "$FAKE_CURL_CALLS" | paste -sd ' ' -; }
sleeps() { paste -sd ' ' - < "$FAKE_SLEEP_CALLS"; }
put_data() { awk -F'\t' '$1 == "PUT" { print $2 }' "$FAKE_CURL_CALLS" | tail -n 1; }

# 1. The branch moved under the first write: read and write again.
run_case "409 then success" 0 \
  $'200\t{"sha":"1111111111111111111111111111111111111111"}' \
  $'409\t{"message":"Formula/baton-example.rb is at a but expected b"}' \
  $'200\t{"sha":"2222222222222222222222222222222222222222"}' \
  $'200\t{"commit":{"html_url":"https://example.invalid/commit/2"}}'
[[ "$(methods)" == "GET PUT GET PUT" ]] || fail "409 case: methods $(methods)"
[[ "$(sleeps)" == "2" ]] || fail "409 case: sleeps $(sleeps)"
[[ "$(put_data | jq -r .sha)" == "2222222222222222222222222222222222222222" ]] || fail "409 case: second write did not carry the re-read sha"
grep -q "succeeded on attempt 2 of 5" "${tmp_dir}/stderr" || fail "409 case: no attempt report"

# 2. A new formula: no sha in the write.
run_case "new formula" 0 \
  $'404\t{"message":"Not Found"}' \
  $'201\t{"commit":{"html_url":"https://example.invalid/commit/new"}}'
[[ "$(methods)" == "GET PUT" ]] || fail "new formula: methods $(methods)"
[[ "$(put_data | jq -r 'has("sha")')" == "false" ]] || fail "new formula: write carried a sha"
[[ "$(put_data | jq -r .message)" == "$COMMIT_MESSAGE" ]] || fail "new formula: message mismatch"
[[ "$(put_data | jq -r .committer.name)" == "goreleaserbot" ]] || fail "new formula: committer mismatch"
[[ "$(put_data | jq -r .content | base64 -d)" == "$(cat "$formula")" ]] || fail "new formula: content round-trip"
[[ ! -s "$FAKE_SLEEP_CALLS" ]] || fail "new formula: slept"

# 3. The tap already has this exact content: nothing is written.
run_case "already current" 0 \
  "200	{\"sha\":\"${formula_sha}\"}"
[[ "$(methods)" == "GET" ]] || fail "already current: methods $(methods)"
grep -q "already has this content" "${tmp_dir}/stderr" || fail "already current: no report"

# 4. A final error is not retried.
run_case "422 is final" 1 \
  $'200\t{"sha":"1111111111111111111111111111111111111111"}' \
  $'422\t{"message":"Validation Failed"}'
[[ "$(methods)" == "GET PUT" ]] || fail "422 case: methods $(methods)"
[[ ! -s "$FAKE_SLEEP_CALLS" ]] || fail "422 case: slept"

# 5. Attempts run out: one fewer sleep than attempts, doubling delay.
HOMEBREW_PUSH_ATTEMPTS=3 run_case "exhausted" 1 \
  $'200\t{"sha":"1"}' $'409\t{}' \
  $'200\t{"sha":"1"}' $'409\t{}' \
  $'200\t{"sha":"1"}' $'409\t{}'
[[ "$(sleeps)" == "2 4" ]] || fail "exhausted: sleeps $(sleeps)"
grep -q "giving up after 3 attempts" "${tmp_dir}/stderr" || fail "exhausted: no give-up report"

# 6. A transport failure on the read is retried too.
run_case "transport failure then success" 0 \
  $'000\t' \
  $'200\t{"sha":"1111111111111111111111111111111111111111"}' \
  $'200\t{"commit":{"html_url":"https://example.invalid/commit/3"}}'
[[ "$(methods)" == "GET GET PUT" ]] || fail "transport case: methods $(methods)"

# 7. A read error that is final stops before any write.
run_case "403 on read is final" 1 \
  $'403\t{"message":"Resource not accessible by integration"}'
[[ "$(methods)" == "GET" ]] || fail "403 case: methods $(methods)"

# 8. Bad retry settings are rejected before any request.
HOMEBREW_PUSH_ATTEMPTS=0 run_case "zero attempts" 2
[[ ! -s "$FAKE_CURL_CALLS" ]] || fail "zero attempts: made a request"

# 9. A missing formula file fails before any request.
FORMULA_FILE="${tmp_dir}/missing.rb" run_case "missing formula" 1
[[ ! -s "$FAKE_CURL_CALLS" ]] || fail "missing formula: made a request"

if [[ "$failures" -gt 0 ]]; then
  echo "${failures} failure(s)" >&2
  exit 1
fi
echo "publish-homebrew-formula tests passed"
