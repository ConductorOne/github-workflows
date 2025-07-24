#!/bin/bash

set -exo pipefail

if [ -z "$BATON_CONNECTOR" ]; then
  echo "BATON_CONNECTOR not set"
  exit 1
fi
if [ -z "$BATON" ]; then
  echo "BATON not set. using baton"
  BATON=baton
fi

if ! command -v $BATON &> /dev/null; then
  echo "$BATON not found"
  exit 1
fi

# Error on unbound variables now that we've set BATON
set -u

# Sync
$BATON_CONNECTOR

set +e
CAPABILITY_PROVISION=$($BATON_CONNECTOR capabilities | jq --raw-output --exit-status '.connectorCapabilities[] | select(contains("CAPABILITY_PROVISION") )')
set -e
if [ -z "$CAPABILITY_PROVISION" ]; then
  $BATON grants --entitlement="$BATON_ENTITLEMENT" --output-format=json | jq --exit-status ".grants[] | select( .principal.id.resource == \"$BATON_PRINCIPAL\" )"
  echo "CAPABILITY_PROVISION not found. Skipping grant/revoke tests."
  exit 0
fi

# Grant entitlement
$BATON_CONNECTOR --grant-entitlement="$BATON_ENTITLEMENT" --grant-principal="$BATON_PRINCIPAL" --grant-principal-type="$BATON_PRINCIPAL_TYPE"

# Check for grant before revoking
$BATON_CONNECTOR
$BATON grants --entitlement="$BATON_ENTITLEMENT" --output-format=json | jq --exit-status ".grants[] | select( .principal.id.resource == \"$BATON_PRINCIPAL\" )"

# Grant already-granted entitlement
$BATON_CONNECTOR --grant-entitlement="$BATON_ENTITLEMENT" --grant-principal="$BATON_PRINCIPAL" --grant-principal-type="$BATON_PRINCIPAL_TYPE"

set +u
# Get grant ID
BATON_GRANT=$($BATON grants --entitlement="$BATON_ENTITLEMENT" --output-format=json | jq --raw-output --exit-status ".grants[] | select( .principal.id.resource == \"$BATON_PRINCIPAL\" ).grant.id")
set -u

# Revoke grant
$BATON_CONNECTOR --revoke-grant="$BATON_GRANT"

# Revoke already-revoked grant
$BATON_CONNECTOR --revoke-grant="$BATON_GRANT"

# Check grant was revoked
$BATON_CONNECTOR
$BATON grants --entitlement="$BATON_ENTITLEMENT" --output-format=json | jq --exit-status "if .grants then [ .grants[] | select( .principal.id.resource == \"$BATON_PRINCIPAL\" ) ] | length == 0 else . end"

# Re-grant entitlement
$BATON_CONNECTOR --grant-entitlement="$BATON_ENTITLEMENT" --grant-principal="$BATON_PRINCIPAL" --grant-principal-type="$BATON_PRINCIPAL_TYPE"

# Check grant was re-granted
$BATON_CONNECTOR
$BATON grants --entitlement="$BATON_ENTITLEMENT" --output-format=json | jq --exit-status ".grants[] | select( .principal.id.resource == \"$BATON_PRINCIPAL\" )"
