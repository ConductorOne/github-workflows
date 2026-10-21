#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/repo/scripts" "$work/repo/.github/actions/ca-trust-guard" "$work/bin" "$work/certs"
ln -s "$root/.github/actions/ca-trust-guard/fixtures" "$work/repo/.github/actions/ca-trust-guard/fixtures"
# Redirect only OS filesystem boundaries; tests can never write host trust data.
sed -e "s@/usr/share/ca-certificates@$work/certs@g" \
    -e "s@/etc/ca-certificates.conf@$work/ca-certificates.conf@g" \
    -e "s@/etc/ssl/certs/ca-certificates.crt@$work/bundle.pem@g" \
    "$root/scripts/deny-os.sh" > "$work/repo/scripts/deny-os.sh"
fixtures="$root/.github/actions/ca-trust-guard/fixtures"
awk -v dir="$work/certs" '/-----BEGIN CERTIFICATE-----/{n++} n {print > sprintf("%s/root-%d.crt", dir, n)}' \
  "$fixtures/vulnerable-roots.pem"
printf 'root-1.crt\nroot-2.crt\nroot-3.crt\nroot-4.crt\n' > "$work/ca-certificates.conf"
cp "$fixtures/vulnerable-roots.pem" "$work/bundle.pem"
export TEST_WORK="$work" TEST_FIXTURES="$fixtures" OPENSSL_REAL
OPENSSL_REAL=$(command -v openssl)
cat > "$work/bin/uname" <<'SCRIPT'
#!/bin/sh
printf '%s\n' "$TEST_OS"
SCRIPT
cat > "$work/bin/id" <<'SCRIPT'
#!/bin/sh
printf '%s\n' "${TEST_UID:-0}"
SCRIPT
cat > "$work/bin/security" <<'SCRIPT'
#!/bin/bash
set -euo pipefail
if [ "$1" = find-certificate ]; then
  if [ "${TEST_NOT_PRESENT:-false}" = true ]; then exit 1; fi
  if [ "${TEST_FALLBACK:-false}" = true ] && [[ "$*" == *SystemRootCertificates* ]]; then exit 1; fi
  for cert in "$TEST_WORK"/certs/*.crt; do
    subject=$("$OPENSSL_REAL" x509 -in "$cert" -noout -subject -nameopt RFC2253)
    case "$subject" in *"CN=$3,"*|*"CN=$3") cat "$cert"; exit 0;; esac
  done
  exit 1
fi
printf 'keychain deny\n' >> "$TEST_WORK/mutations"
exit "${TEST_MUTATION_RC:-0}"
SCRIPT
cat > "$work/bin/openssl" <<'SCRIPT'
#!/bin/bash
set -euo pipefail
if [ "${TEST_MISMATCH:-false}" = true ] && [[ " $* " == *' -fingerprint '* ]]; then
  printf 'Fingerprint=0000000000000000000000000000000000000000\n'
else exec "$OPENSSL_REAL" "$@"; fi
SCRIPT
cat > "$work/bin/update-ca-certificates" <<'SCRIPT'
#!/bin/sh
printf 'update\n' >> "$TEST_WORK/updates"
if [ "${TEST_KEEP_BUNDLE:-false}" != true ]; then
  cp "$TEST_FIXTURES/clean-ca-bundle.pem" "$TEST_WORK/bundle.pem"
fi
SCRIPT
cat > "$work/bin/record" <<'SCRIPT'
#!/bin/sh
printf 'ROW %s\n' "$*"
SCRIPT
cat > "$work/bin/sudo" <<'SCRIPT'
#!/bin/sh
printf '%s\n' "$1" >> "$TEST_WORK/sudo-calls"
[ "$1" = -n ] || exit 99
[ "${TEST_NO_ELEVATION:-false}" != true ] || exit 1
shift
exec "$@"
SCRIPT
chmod +x "$work"/bin/*
export PATH="$work/bin:$PATH" CA_GUARD_BIN="$work/bin/record" CA_GUARD_STATE="$work"
output=$(TEST_OS=Darwin bash "$work/repo/scripts/deny-os.sh" --dry-run)
[ "$(printf '%s\n' "$output" | grep -c 'DRY-RUN security add-trusted-cert')" = 6 ]
[ ! -e "$work/mutations" ]
output=$(TEST_OS=Linux bash "$work/repo/scripts/deny-os.sh" --dry-run)
[ "$(printf '%s\n' "$output" | grep -c 'DRY-RUN cp')" = 4 ]
printf '%s\n' "$output" | grep -q 'DRY-RUN update-ca-certificates'
[ ! -e "$work/updates" ]
[ "$(grep -c '^root-' "$work/ca-certificates.conf")" = 4 ]
for os in Linux Darwin; do
  if TEST_OS="$os" TEST_MISMATCH=true bash "$work/repo/scripts/deny-os.sh" --dry-run > "$work/mismatch" 2>&1; then
    echo 'fingerprint mismatch was not refused' >&2; exit 1
  fi
  grep -q 'fingerprint mismatch' "$work/mismatch"
  [ ! -e "$work/mutations" ]
done
output=$(TEST_OS=Linux bash "$work/repo/scripts/deny-os.sh")
[ "$(grep -c '^!root-' "$work/ca-certificates.conf")" = 4 ]
printf '%s\n' "$output" | grep -q 'VERIFIED-DISTRUST'
TEST_OS=Linux bash "$work/repo/scripts/deny-os.sh" > /dev/null
[ "$(wc -l < "$work/updates" | tr -d ' ')" = 1 ]
cp "$fixtures/vulnerable-roots.pem" "$work/bundle.pem"
if TEST_OS=Linux TEST_KEEP_BUNDLE=true bash "$work/repo/scripts/deny-os.sh" > "$work/residual" 2>&1; then
  echo 'compiled-bundle verification missed a residual root' >&2; exit 1
fi
grep -q 'still trusted after blacklist' "$work/residual"
output=$(TEST_OS=Darwin TEST_UID=1000 TEST_FALLBACK=true bash "$work/repo/scripts/deny-os.sh")
[ "$(wc -l < "$work/mutations" | tr -d ' ')" = 6 ]
[ "$(grep -c '^-n$' "$work/sudo-calls")" = 7 ]
[ "$(printf '%s\n' "$output" | grep -c 'DENIED')" = 6 ]
rm -f "$work/mutations"
output=$(TEST_OS=Darwin TEST_NOT_PRESENT=true bash "$work/repo/scripts/deny-os.sh")
[ "$(printf '%s\n' "$output" | grep -c 'NOT-PRESENT')" = 4 ]
[ "$(wc -l < "$work/mutations" | tr -d ' ')" = 2 ]
rm -f "$work/mutations"
output=$(TEST_OS=Darwin TEST_UID=1000 TEST_NO_ELEVATION=true bash "$work/repo/scripts/deny-os.sh")
printf '%s\n' "$output" | grep -q 'SKIPPED-NOT-ELEVATED'
[ ! -e "$work/mutations" ]
rc=0
TEST_OS=Darwin TEST_MUTATION_RC=7 bash "$work/repo/scripts/deny-os.sh" > "$work/failure" 2>&1 || rc=$?
[ "$rc" = 7 ]
printf 'deny-os tests passed (six identities, dry-run, mismatch, elevation, idempotency, compiled verification)\n'
