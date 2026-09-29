#!/usr/bin/env bash
# validate-release-artifacts.sh - Validates release artifacts and attestations
#
# Usage: validate-release-artifacts.sh ORG/REPO VERSION [RELEASE_STORAGE_NAME]
# Example: validate-release-artifacts.sh ConductorOne/baton-github-test v0.1.102
#
# Validates:
# - Manifest structure and required fields
# - Binary assets exist and are downloadable
# - Provenance attestations exist and verify with cosign
# - SBOM attestations exist and verify with cosign
# - ECR Public image attestation (if present)
#
# Exit codes:
# 0 - All validations passed
# 1 - One or more validations failed

set -euo pipefail

# Constants
BASE_URL="https://dist.conductorone.com/releases"

# cosign verification reaches Rekor and the certificate transparency log,
# which time out and rate-limit under load. A failed verification is retried
# up to COSIGN_VERIFY_ATTEMPTS times, waiting COSIGN_VERIFY_DELAY seconds
# before the second attempt and doubling after that.
COSIGN_VERIFY_ATTEMPTS="${COSIGN_VERIFY_ATTEMPTS:-3}"
COSIGN_VERIFY_DELAY="${COSIGN_VERIFY_DELAY:-10}"
if [[ ! "$COSIGN_VERIFY_ATTEMPTS" =~ ^[1-9][0-9]*$ || ! "$COSIGN_VERIFY_DELAY" =~ ^[0-9]+$ ]]; then
  echo "COSIGN_VERIFY_ATTEMPTS must be a positive integer and COSIGN_VERIFY_DELAY a non-negative integer, got: ${COSIGN_VERIFY_ATTEMPTS} / ${COSIGN_VERIFY_DELAY}" >&2
  exit 1
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# Arguments
ORG_REPO="${1:-}"
VERSION="${2:-}"

if [[ -z "$ORG_REPO" || -z "$VERSION" ]]; then
  echo "Usage: validate-release-artifacts.sh ORG/REPO VERSION [RELEASE_STORAGE_NAME]"
  echo "Example: validate-release-artifacts.sh ConductorOne/baton-github-test v0.1.102"
  exit 1
fi

ORG="${ORG_REPO%%/*}"
REPO="${ORG_REPO#*/}"
if [[ -z "$ORG" || -z "$REPO" || "$ORG" == "$ORG_REPO" ]]; then
  echo "ORG/REPO must be in owner/name form, got: $ORG_REPO" >&2
  exit 1
fi

RELEASE_TARGET_NAME_REGEX='^[a-z][a-z0-9-]{0,99}$'
RELEASE_STORAGE_NAME="${3:-$REPO}"
if [[ ! "$RELEASE_STORAGE_NAME" =~ $RELEASE_TARGET_NAME_REGEX ]]; then
  echo "release_storage_name must match ${RELEASE_TARGET_NAME_REGEX} to align with registry release target validation, got: $RELEASE_STORAGE_NAME" >&2
  exit 1
fi

RELEASE_BASE_URL="${BASE_URL}/${ORG}/${RELEASE_STORAGE_NAME}/${VERSION}"
MANIFEST_URL="${RELEASE_BASE_URL}/manifest.json"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

FAILED=0
PASSED=0

pass() {
  echo -e "${GREEN}✅ $1${NC}"
  PASSED=$((PASSED + 1))
}

fail() {
  echo -e "${RED}❌ $1${NC}"
  FAILED=$((FAILED + 1))
}

warn() {
  echo -e "${YELLOW}⚠️  $1${NC}"
}

info() {
  echo -e "ℹ️  $1"
}

COSIGN_STDERR="$TEMP_DIR/cosign-stderr.log"

# verify_with_retry LABEL COSIGN_ARGS...
# Runs cosign with the given arguments, retrying a failure per the settings
# above. cosign's stderr from the last attempt is left in COSIGN_STDERR for
# fail_verify to print, so the reason for a failure reaches the log.
verify_with_retry() {
  local label="$1"
  shift
  local attempt=1
  local delay=$((10#$COSIGN_VERIFY_DELAY))
  while :; do
    if cosign "$@" > /dev/null 2> "$COSIGN_STDERR"; then
      if [[ $attempt -gt 1 ]]; then
        info "$label: verified on attempt $attempt of $COSIGN_VERIFY_ATTEMPTS"
      fi
      return 0
    fi
    if [[ $attempt -ge $COSIGN_VERIFY_ATTEMPTS ]]; then
      return 1
    fi
    warn "$label: attempt $attempt of $COSIGN_VERIFY_ATTEMPTS failed ($(tail -n 1 "$COSIGN_STDERR")); retrying in ${delay}s"
    sleep "$delay"
    delay=$((delay * 2))
    attempt=$((attempt + 1))
  done
}

# fail_verify MESSAGE: records a failed verification and prints cosign's
# stderr from the final attempt beneath it.
fail_verify() {
  fail "$1"
  sed 's/^/     /' "$COSIGN_STDERR"
}

# Certificate identity pattern for cosign verification
CERT_IDENTITY_REGEXP='https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@.*'
CERT_OIDC_ISSUER='https://token.actions.githubusercontent.com'

echo ""
echo "🔍 Validating release: ${ORG_REPO} ${VERSION}"
echo "   Manifest URL: ${MANIFEST_URL}"
echo ""

# 1. Fetch manifest
echo "=== Manifest Validation ==="
if ! curl -sfL "$MANIFEST_URL" -o "$TEMP_DIR/manifest.json"; then
  fail "Failed to fetch manifest from $MANIFEST_URL"
  echo ""
  echo "Summary: 0 passed, 1 failed"
  exit 1
fi
pass "Manifest fetched successfully"

MANIFEST=$(cat "$TEMP_DIR/manifest.json")

# 2. Validate manifest structure
if ! echo "$MANIFEST" | jq -e '.semver' > /dev/null 2>&1; then
  fail "Manifest missing 'semver' field"
else
  MANIFEST_SEMVER=$(echo "$MANIFEST" | jq -r '.semver')
  if [[ "$MANIFEST_SEMVER" == "$VERSION" ]]; then
    pass "Manifest semver matches tag: $MANIFEST_SEMVER"
  else
    fail "Manifest semver ($MANIFEST_SEMVER) doesn't match tag ($VERSION)"
  fi
fi

if ! echo "$MANIFEST" | jq -e '.assets' > /dev/null 2>&1; then
  fail "Manifest missing 'assets' field"
else
  ASSET_COUNT=$(echo "$MANIFEST" | jq '.assets | length')
  pass "Manifest has $ASSET_COUNT assets"
fi

# 3. Validate each asset
echo ""
echo "=== Binary Asset Validation ==="
for platform in $(echo "$MANIFEST" | jq -r '.assets | keys[]'); do
  HREF=$(echo "$MANIFEST" | jq -r ".assets[\"$platform\"].href")
  FILENAME=$(basename "$HREF")
  
  info "Validating: $platform"
  
  # Check asset exists
  if ! curl -sfIL "$HREF" > /dev/null 2>&1; then
    fail "Asset not found: $HREF"
    continue
  fi
  pass "Asset downloadable: $platform"
  
  # Download asset for verification
  if ! curl -sfL "$HREF" -o "$TEMP_DIR/$FILENAME" 2>/dev/null; then
    fail "Failed to download asset: $HREF"
    continue
  fi
  
  # Check binary signature (.sig + .cert files)
  SIG_FILE="${HREF}.sig"
  CERT_FILE="${HREF}.cert"
  if curl -sfL "$SIG_FILE" -o "$TEMP_DIR/${FILENAME}.sig" 2>/dev/null && \
     curl -sfL "$CERT_FILE" -o "$TEMP_DIR/${FILENAME}.cert" 2>/dev/null; then
    if verify_with_retry "binary signature $platform" verify-blob \
      --signature "$TEMP_DIR/${FILENAME}.sig" \
      --certificate "$TEMP_DIR/${FILENAME}.cert" \
      --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
      --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
      "$TEMP_DIR/$FILENAME"; then
      pass "Binary signature verified: $platform"
    else
      fail_verify "Binary signature verification failed: $platform"
    fi
  else
    fail "Binary signature files missing: $platform (.sig or .cert)"
  fi

  # Check provenance attestation
  PROV_BUNDLE="${HREF}.provenance.sigstore.json"
  if ! curl -sfL "$PROV_BUNDLE" -o "$TEMP_DIR/${FILENAME}.provenance.sigstore.json" 2>/dev/null; then
    fail "Provenance bundle missing: $PROV_BUNDLE"
  else
    # Verify provenance
    if verify_with_retry "provenance $platform" verify-blob-attestation \
      --bundle "$TEMP_DIR/${FILENAME}.provenance.sigstore.json" \
      --type https://slsa.dev/provenance/v1 \
      --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
      --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
      "$TEMP_DIR/$FILENAME"; then
      pass "Provenance verified: $platform"
    else
      fail_verify "Provenance verification failed: $platform"
    fi
  fi

  # Check SBOM attestation
  if [[ "$platform" == "checksums" ]]; then
    info "Skipping SBOM check for $platform (not applicable)"
  else
    SBOM_BUNDLE="${HREF}.sbom.sigstore.json"
    if ! curl -sfL "$SBOM_BUNDLE" -o "$TEMP_DIR/${FILENAME}.sbom.sigstore.json" 2>/dev/null; then
      fail "SBOM bundle missing: $SBOM_BUNDLE"
    else
      # Verify SBOM
      if verify_with_retry "SBOM $platform" verify-blob-attestation \
        --bundle "$TEMP_DIR/${FILENAME}.sbom.sigstore.json" \
        --type https://spdx.dev/Document \
        --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
        --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
        "$TEMP_DIR/$FILENAME"; then
        pass "SBOM verified: $platform"
      else
        fail_verify "SBOM verification failed: $platform"
      fi
    fi
  fi
  
  # Clean up asset to save disk space
  rm -f "$TEMP_DIR/$FILENAME"
done

echo ""
echo "=== Container Image Validation ==="
# 4. Validate ECR Public image attestation (if present)
ECR_URI=$(echo "$MANIFEST" | jq -r '.images.ecrPublic.uri // empty')
if [[ -n "$ECR_URI" ]]; then
  info "Validating ECR Public image: $ECR_URI"
  if verify_with_retry "ECR Public image attestation" verify-attestation \
    --type https://slsa.dev/provenance/v1 \
    --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
    --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
    "$ECR_URI"; then
    pass "ECR Public image attestation verified"
  else
    fail_verify "ECR Public image attestation verification failed"
  fi
else
  warn "No ECR Public image in manifest (docker may have been skipped)"
fi

# 5. Validate manifest signature (if present)
echo ""
echo "=== Manifest Signature Validation ==="
MANIFEST_SIG_URL="${RELEASE_BASE_URL}/manifest.json.sig"
MANIFEST_CERT_URL="${RELEASE_BASE_URL}/manifest.json.cert"
MANIFEST_BUNDLE_URL="${RELEASE_BASE_URL}/manifest.json.sigstore.json"

if curl -sfL "$MANIFEST_BUNDLE_URL" -o "$TEMP_DIR/manifest.json.sigstore.json" 2>/dev/null; then
  if verify_with_retry "manifest bundle" verify-blob \
    --bundle "$TEMP_DIR/manifest.json.sigstore.json" \
    --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
    --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
    "$TEMP_DIR/manifest.json"; then
    pass "Manifest Sigstore bundle verified"
  else
    fail_verify "Manifest Sigstore bundle verification failed"
  fi
elif curl -sfL "$MANIFEST_SIG_URL" -o "$TEMP_DIR/manifest.json.sig" 2>/dev/null && \
   curl -sfL "$MANIFEST_CERT_URL" -o "$TEMP_DIR/manifest.json.cert" 2>/dev/null; then
  if verify_with_retry "manifest signature" verify-blob \
    --signature "$TEMP_DIR/manifest.json.sig" \
    --certificate "$TEMP_DIR/manifest.json.cert" \
    --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
    --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
    "$TEMP_DIR/manifest.json"; then
    pass "Manifest signature verified"
  else
    fail_verify "Manifest signature verification failed"
  fi
else
  fail "Manifest signature files not found"
fi

# Summary
echo ""
echo "========================================"
if [[ $FAILED -eq 0 ]]; then
  echo -e "${GREEN}🎉 All validations passed!${NC}"
  echo "   Passed: $PASSED"
  exit 0
else
  echo -e "${RED}❌ Some validations failed${NC}"
  echo "   Passed: $PASSED"
  echo "   Failed: $FAILED"
  exit 1
fi
