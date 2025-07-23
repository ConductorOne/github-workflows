#!/usr/bin/env bash

set -euxo pipefail

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
if [ "${ARCH}" = "x86_64" ]; then
  ARCH="amd64"
fi

RELEASES_URL="https://api.github.com/repos/conductorone/baton/releases/latest"
BASE_URL="https://github.com/conductorone/baton/releases/download"

curl_opts=(--fail-with-body "${RELEASES_URL}")
if [ -n "${GITHUB_TOKEN:-}" ]; then
  curl_opts+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
fi
DOWNLOAD_URL=$(curl "${curl_opts[@]}" | jq --raw-output ".assets[].browser_download_url | match(\"${BASE_URL}/v[.0-9]+/baton-v[.0-9]+-${OS}-${ARCH}.*\"; \"i\").string")
FILENAME=$(basename ${DOWNLOAD_URL})

curl -LO ${DOWNLOAD_URL}
tar xzf ${FILENAME}

mv baton /usr/local/bin/baton
