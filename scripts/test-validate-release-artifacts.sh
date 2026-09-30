#!/usr/bin/env bash
# Exercises validate-release-artifacts.sh's cosign retry against stubbed
# cosign and curl binaries placed ahead of the real ones on PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

stub_dir="${tmp_dir}/bin"
mkdir -p "$stub_dir"

# One asset, no image: four cosign verifications per run (binary signature,
# provenance, SBOM, manifest bundle).
manifest="${tmp_dir}/manifest.json"
cat > "$manifest" <<'MANIFEST'
{
  "semver": "v1.2.3",
  "assets": {
    "linux-amd64": {
      "href": "https://dist.conductorone.com/releases/ConductorOne/example/v1.2.3/example-v1.2.3-linux-amd64.tar.gz"
    }
  }
}
MANIFEST

# curl stub: every fetch succeeds; -o writes the manifest for manifest.json
# and a placeholder for everything else.
cat > "${stub_dir}/curl" <<'FAKE_CURL'
#!/usr/bin/env bash
out=""
url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
if [[ -n "$out" ]]; then
  case "$url" in
    *manifest.json) cat "$FAKE_MANIFEST" > "$out" ;;
    *) printf 'stub\n' > "$out" ;;
  esac
fi
exit 0
FAKE_CURL

# cosign stub: counts calls, fails the first FAKE_COSIGN_FAIL_FIRST of them
# with a Rekor-style error on stderr, succeeds afterwards.
cat > "${stub_dir}/cosign" <<'FAKE_COSIGN'
#!/usr/bin/env bash
n=0
if [[ -f "$FAKE_COSIGN_CALLS" ]]; then
  n="$(cat "$FAKE_COSIGN_CALLS")"
fi
n=$((n + 1))
printf '%s\n' "$n" > "$FAKE_COSIGN_CALLS"
if [[ "$n" -le "${FAKE_COSIGN_FAIL_FIRST:-0}" ]]; then
  echo "Error: verifying blob: rekor: 429 Too Many Requests" >&2
  exit 1
fi
echo "Verified OK" >&2
exit 0
FAKE_COSIGN
chmod +x "${stub_dir}/curl" "${stub_dir}/cosign"

calls="${tmp_dir}/cosign-calls"
sleep_calls="${tmp_dir}/sleep-calls"
cat > "${stub_dir}/sleep" <<'FAKE_SLEEP'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$FAKE_SLEEP_CALLS"
FAKE_SLEEP
chmod +x "${stub_dir}/sleep"

# run_validate FAIL_FIRST ATTEMPTS [DELAY]: runs the script, records its exit status
# in $status and its output in $output, and the cosign call count in $count.
run_validate() {
  rm -f "$calls" "$sleep_calls"
  set +e
  output="$(PATH="${stub_dir}:${PATH}" FAKE_MANIFEST="$manifest" FAKE_COSIGN_CALLS="$calls" \
    FAKE_COSIGN_FAIL_FIRST="$1" COSIGN_VERIFY_ATTEMPTS="$2" COSIGN_VERIFY_DELAY="${3:-0}" FAKE_SLEEP_CALLS="$sleep_calls" \
    "${ROOT_DIR}/scripts/validate-release-artifacts.sh" ConductorOne/example v1.2.3 2>&1)"
  status=$?
  set -e
  count="$(cat "$calls" 2>/dev/null || echo 0)"
}

expect() {
  if ! grep -Fq -- "$1" <<< "$output"; then
    echo "expected output to contain: $1" >&2
    echo "$output" >&2
    exit 1
  fi
}

# A transient failure on the first verification passes on its retry.
run_validate 1 3
if [[ $status -ne 0 ]]; then
  echo "one transient cosign failure should not fail validation (status $status)" >&2
  echo "$output" >&2
  exit 1
fi
expect "binary signature linux-amd64: attempt 1 of 3 failed (Error: verifying blob: rekor: 429 Too Many Requests); retrying in 0s"
expect "binary signature linux-amd64: verified on attempt 2 of 3"
expect "All validations passed"
if [[ "$count" -ne 5 ]]; then
  echo "expected 5 cosign calls (4 verifications + 1 retry), got $count" >&2
  exit 1
fi

# The same transient failure with retries disabled fails, which is what
# proves the retry above did the work.
run_validate 1 1
if [[ $status -eq 0 ]]; then
  echo "a cosign failure with one attempt should fail validation" >&2
  exit 1
fi
expect "Binary signature verification failed: linux-amd64"
if [[ "$count" -ne 4 ]]; then
  echo "expected 4 cosign calls with retries disabled, got $count" >&2
  exit 1
fi

# A persistent failure exhausts every attempt on every verification and
# surfaces cosign's stderr under each failure line.
run_validate 1000 3
if [[ $status -eq 0 ]]; then
  echo "persistent cosign failures should fail validation" >&2
  exit 1
fi
expect "Provenance verification failed: linux-amd64"
expect "     Error: verifying blob: rekor: 429 Too Many Requests"
expect "Failed: 4"
if [[ "$count" -ne 12 ]]; then
  echo "expected 12 cosign calls (4 verifications x 3 attempts), got $count" >&2
  exit 1
fi

# A non-numeric attempt count is rejected up front instead of looping.
run_validate 0 abc
if [[ $status -eq 0 ]]; then
  echo "a non-numeric COSIGN_VERIFY_ATTEMPTS should fail validation" >&2
  exit 1
fi
expect "COSIGN_VERIFY_ATTEMPTS must be a positive integer"
if [[ "$count" -ne 0 ]]; then
  echo "expected no cosign calls with an invalid attempt count, got $count" >&2
  exit 1
fi

for delay in 08 010 10; do
  run_validate 2 3 "$delay"
  if [[ $status -ne 0 || "$count" -ne 6 ]]; then
    echo "decimal delay $delay should recover on the third attempt (status $status, calls $count)" >&2
    echo "$output" >&2
    exit 1
  fi
  decimal=$((10#$delay))
  expected=$(printf '%s\n%s' "$decimal" "$((decimal * 2))")
  if [[ "$(cat "$sleep_calls")" != "$expected" ]]; then
    echo "wrong backoff for delay $delay: $(cat "$sleep_calls")" >&2
    exit 1
  fi
done

echo "validate-release-artifacts retry tests passed"
