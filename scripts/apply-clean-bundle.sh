#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bundle="$root/.github/actions/ca-trust-guard/fixtures/clean-ca-bundle.pem"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    bundle=$(cygpath -w "$bundle")
    export GIT_SSL_CAINFO="$bundle" CURL_CA_BUNDLE="$bundle"
    printf 'GIT_SSL_CAINFO=%s\nCURL_CA_BUNDLE=%s\n' "$bundle" "$bundle" >> "$GITHUB_ENV"
    git config --global http.sslCAInfo "$bundle" ;;
  *)
    export SSL_CERT_FILE="$bundle" CURL_CA_BUNDLE="$bundle" GIT_SSL_CAINFO="$bundle"
    export REQUESTS_CA_BUNDLE="$bundle" AWS_CA_BUNDLE="$bundle" NPM_CONFIG_CAFILE="$bundle"
    for name in SSL_CERT_FILE CURL_CA_BUNDLE GIT_SSL_CAINFO REQUESTS_CA_BUNDLE AWS_CA_BUNDLE NPM_CONFIG_CAFILE; do
      printf '%s=%s\n' "$name" "$bundle" >> "$GITHUB_ENV"
    done
    git config --global http.sslCAInfo "$bundle"
    git config --global http.sslVerify true ;;
esac
# NODE_OPTIONS is deliberately NOT written: the runner forbids it via GITHUB_ENV.
