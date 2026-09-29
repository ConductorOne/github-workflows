#!/usr/bin/env bash
# cosign-retry.sh - Runs cosign, retrying a failed invocation.
#
# Usage: cosign-retry.sh COSIGN_ARGS...
# Example: cosign-retry.sh attest-blob --yes --predicate p.json --type slsaprovenance1 --bundle out.json artifact.tar.gz
#
# Keyless signing asks GitHub's OIDC endpoint for a token and Fulcio for a
# certificate on every call, and both fail transiently under load. Signing is
# safe to repeat: a retry requests a fresh token and certificate, and a failed
# attempt leaves nothing behind but an unused Rekor entry. stdout and stderr
# pass through unchanged; the exit status is cosign's from the final attempt.
#
# COSIGN_RETRY_ATTEMPTS (default 3) and COSIGN_RETRY_DELAY (default 10, the
# seconds before the second attempt, doubling after that) tune the retry.

set -euo pipefail

attempts="${COSIGN_RETRY_ATTEMPTS:-3}"
delay="${COSIGN_RETRY_DELAY:-10}"
if [[ ! "$attempts" =~ ^[1-9][0-9]*$ || ! "$delay" =~ ^[0-9]+$ ]]; then
  echo "cosign-retry: COSIGN_RETRY_ATTEMPTS must be a positive integer and COSIGN_RETRY_DELAY a non-negative integer, got: ${attempts} / ${delay}" >&2
  exit 2
fi
delay=$((10#$delay))

if [[ $# -eq 0 ]]; then
  echo "Usage: cosign-retry.sh COSIGN_ARGS..." >&2
  exit 2
fi

attempt=1
while :; do
  status=0
  cosign "$@" || status=$?
  if [[ $status -eq 0 ]]; then
    if [[ $attempt -gt 1 ]]; then
      echo "cosign-retry: succeeded on attempt $attempt of $attempts" >&2
    fi
    exit 0
  fi
  if [[ $attempt -ge $attempts ]]; then
    exit "$status"
  fi
  echo "cosign-retry: attempt $attempt of $attempts failed (exit $status); retrying in ${delay}s" >&2
  sleep "$delay"
  delay=$((delay * 2))
  attempt=$((attempt + 1))
done
