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
CAPABILITY_ACCOUNT_PROVISIONING=$($BATON_CONNECTOR capabilities | jq --raw-output --exit-status '.connectorCapabilities[] | select(contains("CAPABILITY_ACCOUNT_PROVISIONING") )')
CAPABILITY_RESOURCE_DELETE=$($BATON_CONNECTOR capabilities | jq --raw-output --exit-status '.connectorCapabilities[] | select(contains("CAPABILITY_RESOURCE_DELETE") )')
set -e

if [ -z "$CAPABILITY_ACCOUNT_PROVISIONING" ]; then
  echo "CAPABILITY_ACCOUNT_PROVISIONING not found. Skipping account provisioning tests."
  exit 0
fi

if [ -z "$CAPABILITY_RESOURCE_DELETE" ]; then
  echo "CAPABILITY_RESOURCE_DELETE not found. Skipping account deprovisioning tests."
  exit 0
fi

# create -> check -> delete -> delete again -> check

# create account
$BATON_CONNECTOR --create-account-login="$BATON_ACCOUNT_LOGIN" \
                 --create-account-email="$BATON_ACCOUNT_EMAIL" \
                 --create-account-profile="$BATON_ACCOUNT_PROFILE"

# check if account was created
$BATON_CONNECTOR
CREATED_ACCOUNT_ID=$($BATON resources -t user --output-format=json \
  | jq -r \
       --arg display_name "$BATON_DISPLAY_NAME" \
       --arg login "$BATON_ACCOUNT_LOGIN" \
       --arg email "$BATON_ACCOUNT_EMAIL" \
       '.resources[] |
          select(
            .resource.displayName == $display_name or
            .resource.displayName == $login or
            .resource.displayName == $email
          ) |
          .resource.id.resource')

if [ -n "$CREATED_ACCOUNT_ID" ]; then
  echo "Account created successfully with ID: $CREATED_ACCOUNT_ID"
else
  echo "Failed to create account"
  exit 1
fi

# delete account
$BATON_CONNECTOR --delete-resource "$CREATED_ACCOUNT_ID" --delete-resource-type "$BATON_ACCOUNT_TYPE"

# delete account already deleted
$BATON_CONNECTOR --delete-resource "$CREATED_ACCOUNT_ID" --delete-resource-type "$BATON_ACCOUNT_TYPE"

# check if account was deleted
$BATON_CONNECTOR
$BATON principals --output-format=json \
  | jq -e \
       --arg name "$BATON_ACCOUNT_LOGIN" \
       '.resources | map(select(.resource.annotations[0].profile.username == $name)) | length == 0'