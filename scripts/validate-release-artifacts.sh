#!/usr/bin/env bash
# validate-release-artifacts.sh - Validates release artifacts and attestations
#
# Usage: validate-release-artifacts.sh ORG/REPO VERSION
# Example: validate-release-artifacts.sh ConductorOne/baton-github-test v0.1.102
#
# Validates:
# - Manifest structure and required fields
# - Binary assets exist and are downloadable
# - Provenance attestations exist and verify with cosign
# - SBOM attestations exist and verify with cosign
# - GHCR image attestation (if present)
# - ECR Public image attestation (if present)
#
# Exit codes:
# 0 - All validations passed
# 1 - One or more validations failed

set -euo pipefail

# Constants
BASE_URL="https://dist.conductorone.com/releases"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# Arguments
ORG_REPO="${1:-}"
VERSION="${2:-}"

if [[ -z "$ORG_REPO" || -z "$VERSION" ]]; then
  echo "Usage: validate-release-artifacts.sh ORG/REPO VERSION"
  echo "Example: validate-release-artifacts.sh ConductorOne/baton-github-test v0.1.102"
  exit 1
fi

MANIFEST_URL="${BASE_URL}/${ORG_REPO}/${VERSION}/manifest.json"
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
  
  # Check binary signature (.sig + .cert files) - skip for MSI (derived artifact)
  if [[ "$platform" == *-msi ]]; then
    info "Skipping .sig/.cert check for $platform (derived artifact)"
  else
    SIG_FILE="${HREF}.sig"
    CERT_FILE="${HREF}.cert"
    if curl -sfL "$SIG_FILE" -o "$TEMP_DIR/${FILENAME}.sig" 2>/dev/null && \
       curl -sfL "$CERT_FILE" -o "$TEMP_DIR/${FILENAME}.cert" 2>/dev/null; then
      if cosign verify-blob \
        --signature "$TEMP_DIR/${FILENAME}.sig" \
        --certificate "$TEMP_DIR/${FILENAME}.cert" \
        --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
        --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
        "$TEMP_DIR/$FILENAME" > /dev/null 2>&1; then
        pass "Binary signature verified: $platform"
      else
        fail "Binary signature verification failed: $platform"
      fi
    else
      fail "Binary signature files missing: $platform (.sig or .cert)"
    fi
  fi

  # Check provenance attestation (skip for MSI - it's derived from the same binary as the zip)
  if [[ "$platform" == *-msi ]]; then
    info "Skipping provenance check for $platform (derived from zip)"
  else
    PROV_BUNDLE="${HREF}.provenance.sigstore.json"
    if ! curl -sfL "$PROV_BUNDLE" -o "$TEMP_DIR/${FILENAME}.provenance.sigstore.json" 2>/dev/null; then
      fail "Provenance bundle missing: $PROV_BUNDLE"
    else
      # Verify provenance
      if cosign verify-blob-attestation \
        --bundle "$TEMP_DIR/${FILENAME}.provenance.sigstore.json" \
        --type https://slsa.dev/provenance/v1 \
        --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
        --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
        "$TEMP_DIR/$FILENAME" > /dev/null 2>&1; then
        pass "Provenance verified: $platform"
      else
        fail "Provenance verification failed: $platform"
      fi
    fi
  fi

  # Check SBOM attestation (skip for checksums and MSI - only binary archives have SBOMs)
  if [[ "$platform" == "checksums" || "$platform" == *-msi ]]; then
    info "Skipping SBOM check for $platform (not applicable)"
  else
    SBOM_BUNDLE="${HREF}.sbom.sigstore.json"
    if ! curl -sfL "$SBOM_BUNDLE" -o "$TEMP_DIR/${FILENAME}.sbom.sigstore.json" 2>/dev/null; then
      fail "SBOM bundle missing: $SBOM_BUNDLE"
    else
      # Verify SBOM
      if cosign verify-blob-attestation \
        --bundle "$TEMP_DIR/${FILENAME}.sbom.sigstore.json" \
        --type https://spdx.dev/Document \
        --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
        --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
        "$TEMP_DIR/$FILENAME" > /dev/null 2>&1; then
        pass "SBOM verified: $platform"
      else
        fail "SBOM verification failed: $platform"
      fi
    fi
  fi
  
  # Clean up asset to save disk space
  rm -f "$TEMP_DIR/$FILENAME"
done

# 4. Validate GHCR image attestation (if present)
echo ""
echo "=== Container Image Validation ==="
GHCR_URI=$(echo "$MANIFEST" | jq -r '.images.ghcr.uri // empty')
if [[ -n "$GHCR_URI" ]]; then
  info "Validating GHCR image: $GHCR_URI"
  if cosign verify-attestation \
    --type https://slsa.dev/provenance/v1 \
    --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
    --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
    "$GHCR_URI" > /dev/null 2>&1; then
    pass "GHCR image attestation verified"
  else
    fail "GHCR image attestation verification failed"
  fi
else
  warn "No GHCR image in manifest (docker may have been skipped)"
fi

# 5. Validate ECR Public image attestation (if present)
ECR_URI=$(echo "$MANIFEST" | jq -r '.images.ecrPublic.uri // empty')
if [[ -n "$ECR_URI" ]]; then
  info "Validating ECR Public image: $ECR_URI"
  if cosign verify-attestation \
    --type https://slsa.dev/provenance/v1 \
    --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
    --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
    "$ECR_URI" > /dev/null 2>&1; then
    pass "ECR Public image attestation verified"
  else
    fail "ECR Public image attestation verification failed"
  fi
else
  warn "No ECR Public image in manifest (docker may have been skipped)"
fi

# 6. Validate manifest signature (if present)
echo ""
echo "=== Manifest Signature Validation ==="
MANIFEST_SIG_URL="${BASE_URL}/${ORG_REPO}/${VERSION}/manifest.json.sig"
MANIFEST_CERT_URL="${BASE_URL}/${ORG_REPO}/${VERSION}/manifest.json.cert"

if curl -sfL "$MANIFEST_SIG_URL" -o "$TEMP_DIR/manifest.json.sig" 2>/dev/null && \
   curl -sfL "$MANIFEST_CERT_URL" -o "$TEMP_DIR/manifest.json.cert" 2>/dev/null; then
  if cosign verify-blob \
    --signature "$TEMP_DIR/manifest.json.sig" \
    --certificate "$TEMP_DIR/manifest.json.cert" \
    --certificate-oidc-issuer "$CERT_OIDC_ISSUER" \
    --certificate-identity-regexp "$CERT_IDENTITY_REGEXP" \
    "$TEMP_DIR/manifest.json" > /dev/null 2>&1; then
    pass "Manifest signature verified"
  else
    fail "Manifest signature verification failed"
  fi
else
  warn "Manifest signature files not found (may be legacy release)"
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
