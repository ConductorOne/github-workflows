#!/usr/bin/env bash
# Exercises cosign-retry.sh against stubbed cosign and sleep binaries placed
# ahead of the real ones on PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

stub_dir="${tmp_dir}/bin"
mkdir -p "$stub_dir"

# cosign stub: counts calls, records its arguments, fails the first
# FAKE_COSIGN_FAIL_FIRST calls with exit 3 and an OIDC-style error on stderr,
# then succeeds and prints on stdout.
cat > "${stub_dir}/cosign" <<'FAKE_COSIGN'
#!/usr/bin/env bash
n=0
if [[ -f "$FAKE_COSIGN_CALLS" ]]; then
  n="$(cat "$FAKE_COSIGN_CALLS")"
fi
n=$((n + 1))
printf '%s\n' "$n" > "$FAKE_COSIGN_CALLS"
printf '%s\n' "$@" > "$FAKE_COSIGN_ARGS"
if [[ "$n" -le "${FAKE_COSIGN_FAIL_FIRST:-0}" ]]; then
  echo "Error: getting signer: getting key from Fulcio: fetching ambient OIDC credentials: invalid character 'u' looking for beginning of value" >&2
  exit 3
fi
echo "signed"
exit 0
FAKE_COSIGN

cat > "${stub_dir}/sleep" <<'FAKE_SLEEP'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$FAKE_SLEEP_CALLS"
FAKE_SLEEP
chmod +x "${stub_dir}/cosign" "${stub_dir}/sleep"

calls="${tmp_dir}/cosign-calls"
args="${tmp_dir}/cosign-args"
sleep_calls="${tmp_dir}/sleep-calls"

# run_retry FAIL_FIRST ATTEMPTS DELAY ARGS...: runs the wrapper with the
# given cosign arguments, capturing exit status, stdout, stderr, and the
# cosign call count.
run_retry() {
  local fail_first="$1" attempts="$2" delay="$3"
  shift 3
  rm -f "$calls" "$args" "$sleep_calls"
  set +e
  stdout="$(PATH="${stub_dir}:${PATH}" FAKE_COSIGN_CALLS="$calls" FAKE_COSIGN_ARGS="$args" \
    FAKE_COSIGN_FAIL_FIRST="$fail_first" FAKE_SLEEP_CALLS="$sleep_calls" \
    COSIGN_RETRY_ATTEMPTS="$attempts" COSIGN_RETRY_DELAY="$delay" \
    "${ROOT_DIR}/scripts/cosign-retry.sh" "$@" 2> "${tmp_dir}/stderr")"
  status=$?
  set -e
  stderr="$(cat "${tmp_dir}/stderr")"
  count="$(cat "$calls" 2>/dev/null || echo 0)"
}

expect_stderr() {
  if ! grep -Fq -- "$1" <<< "$stderr"; then
    echo "expected stderr to contain: $1" >&2
    echo "$stderr" >&2
    exit 1
  fi
}

# One transient failure succeeds on the retry; arguments and stdout pass
# through untouched.
run_retry 1 3 10 attest-blob --yes --predicate "p.json" --type slsaprovenance1 --bundle "out with space.json" artifact.tar.gz
if [[ $status -ne 0 ]]; then
  echo "one transient failure should succeed on retry (status $status)" >&2
  echo "$stderr" >&2
  exit 1
fi
if [[ "$stdout" != "signed" ]]; then
  echo "cosign stdout was not passed through: $stdout" >&2
  exit 1
fi
expect_stderr "cosign-retry: attempt 1 of 3 failed (exit 3); retrying in 10s"
expect_stderr "cosign-retry: succeeded on attempt 2 of 3"
if [[ "$count" -ne 2 ]]; then
  echo "expected 2 cosign calls, got $count" >&2
  exit 1
fi
expected_args="$(printf '%s\n' attest-blob --yes --predicate "p.json" --type slsaprovenance1 --bundle "out with space.json" artifact.tar.gz)"
if [[ "$(cat "$args")" != "$expected_args" ]]; then
  echo "cosign arguments were not passed through verbatim:" >&2
  cat "$args" >&2
  exit 1
fi
if [[ "$(cat "$sleep_calls")" != "10" ]]; then
  echo "expected one sleep of 10, got: $(cat "$sleep_calls")" >&2
  exit 1
fi

# With one attempt the same failure is final, which is what proves the retry
# above did the work; cosign's exit status comes back unchanged.
run_retry 1 1 10 sign-blob --yes manifest.json
if [[ $status -ne 3 || "$count" -ne 1 ]]; then
  echo "expected cosign's exit 3 after a single attempt, got status $status after $count call(s)" >&2
  exit 1
fi
if [[ -f "$sleep_calls" ]]; then
  echo "no sleep expected with a single attempt" >&2
  exit 1
fi

# A persistent failure exhausts the attempts with doubling waits.
run_retry 1000 3 08 attest --yes --type https://slsa.dev/provenance/v1 --predicate p.json image@sha256:abc
if [[ $status -ne 3 || "$count" -ne 3 ]]; then
  echo "expected exit 3 after 3 attempts, got status $status after $count call(s)" >&2
  exit 1
fi
expect_stderr "cosign-retry: attempt 2 of 3 failed (exit 3); retrying in 16s"
expect_stderr "Error: getting signer: getting key from Fulcio"
if [[ "$(cat "$sleep_calls")" != "$(printf '8\n16')" ]]; then
  echo "wrong backoff: $(cat "$sleep_calls")" >&2
  exit 1
fi

# Bad settings and a missing command are rejected before cosign runs.
run_retry 0 abc 10 sign-blob --yes manifest.json
if [[ $status -ne 2 || "$count" -ne 0 ]]; then
  echo "a non-numeric attempt count should exit 2 without calling cosign (status $status, calls $count)" >&2
  exit 1
fi
expect_stderr "COSIGN_RETRY_ATTEMPTS must be a positive integer"
run_retry 0 3 10
if [[ $status -ne 2 || "$count" -ne 0 ]]; then
  echo "no arguments should exit 2 without calling cosign (status $status, calls $count)" >&2
  exit 1
fi
expect_stderr "Usage: cosign-retry.sh COSIGN_ARGS..."

echo "cosign-retry tests passed"
