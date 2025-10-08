#!/bin/bash

set -exo pipefail

if [ -z "$BATON_CONNECTOR" ]; then
  echo "BATON_CONNECTOR not set"
  exit 1
fi

# Error on unbound variables now that we've set BATON
set -u

# Function to search for users by email
search_by_email() {
  local search_value="$1"
  baton resources -t "$ACCOUNT_TYPE" --output-format=json \
    | jq -r --arg email "$search_value" \
         '.resources[] |
          select(
            (.resource.annotations[]? | select(."@type" == "type.googleapis.com/c1.connector.v2.UserTrait")) as $trait |
            $trait != null and ($trait.emails[]?.address // empty) == $email
          ) |
          .resource.id.resource'
}

# Function to search for users by login
search_by_login() {
  local search_value="$1"
  baton resources -t "$ACCOUNT_TYPE" --output-format=json \
    | jq -r --arg login "$search_value" \
         '.resources[] |
          select(
            (.resource.annotations[]? | select(."@type" == "type.googleapis.com/c1.connector.v2.UserTrait")) as $trait |
            $trait != null and $trait.login == $login
          ) |
          .resource.id.resource'
}

# Function to search for users by display name
search_by_display_name() {
  local search_value="$1"
  baton resources -t "$ACCOUNT_TYPE" --output-format=json \
    | jq -r --arg display_name "$search_value" \
         '.resources[] |
          select(.resource.displayName == $display_name) |
          .resource.id.resource'
}

# Function to search for users based on the specified method
search_user() {
  local search_method="$1"
  local search_value="$2"
  
  case "$search_method" in
    "email")
      search_by_email "$search_value"
      ;;
    "login")
      search_by_login "$search_value"
      ;;
    "display_name")
      search_by_display_name "$search_value"
      ;;
    *)
      echo "Error: Invalid search method '$search_method'. Must be one of: email, login, display_name"
      exit 1
      ;;
  esac
}

# Function to get the search value based on the search method
get_search_value() {
  local search_method="$1"
  
  case "$search_method" in
    "email")
      echo "$ACCOUNT_EMAIL"
      ;;
    "login")
      if [ -z "$ACCOUNT_LOGIN" ]; then
        echo "Error: ACCOUNT_LOGIN is required when using 'login' search method"
        exit 1
      fi
      echo "$ACCOUNT_LOGIN"
      ;;
    "display_name")
      if [ -z "$ACCOUNT_DISPLAY_NAME" ]; then
        echo "Error: ACCOUNT_DISPLAY_NAME is required when using 'display_name' search method"
        exit 1
      fi
      echo "$ACCOUNT_DISPLAY_NAME"
      ;;
    *)
      echo "Error: Invalid search method '$search_method'"
      exit 1
      ;;
  esac
}

# Function to create an account with the appropriate parameters
create_account() {
  echo "Creating account with email: $ACCOUNT_EMAIL"
  
  if [ -n "$ACCOUNT_LOGIN" ] && [ -n "$ACCOUNT_PROFILE" ]; then
    echo "Creating account with login, email, and profile"
    $BATON_CONNECTOR --create-account-login="$ACCOUNT_LOGIN" \
                      --create-account-email="$ACCOUNT_EMAIL" \
                      --create-account-profile="$ACCOUNT_PROFILE"
  elif [ -n "$ACCOUNT_PROFILE" ]; then
    echo "Creating account with email and profile"
    $BATON_CONNECTOR --create-account-email="$ACCOUNT_EMAIL" \
                      --create-account-profile="$ACCOUNT_PROFILE"
  elif [ -n "$ACCOUNT_LOGIN" ]; then
    echo "Creating account with login and email"
    $BATON_CONNECTOR --create-account-login="$ACCOUNT_LOGIN" \
                      --create-account-email="$ACCOUNT_EMAIL"
  else
    echo "Creating account with email only"
    $BATON_CONNECTOR --create-account-email="$ACCOUNT_EMAIL"
  fi
}

# Sync
$BATON_CONNECTOR

set +e
CAPABILITY_ACCOUNT_PROVISIONING=$($BATON_CONNECTOR capabilities | jq --raw-output --exit-status '.connectorCapabilities[] | select(contains("CAPABILITY_ACCOUNT_PROVISIONING") )')
CAPABILITY_RESOURCE_DELETE=$($BATON_CONNECTOR capabilities | jq --raw-output --exit-status '.connectorCapabilities[] | select(contains("CAPABILITY_RESOURCE_DELETE") )')
CAPABILITY_CREDENTIAL_ROTATION=$($BATON_CONNECTOR capabilities | jq --raw-output --exit-status '.connectorCapabilities[] | select(contains("CAPABILITY_CREDENTIAL_ROTATION") )')
set -e

if [ -z "$CAPABILITY_ACCOUNT_PROVISIONING" ]; then
  echo "CAPABILITY_ACCOUNT_PROVISIONING not found. Skipping account provisioning tests."
  exit 0
fi

# check if account exists -> create if not exists -> check -> delete -> delete again -> check

# Get the search value based on the search method
SEARCH_VALUE=$(get_search_value "$SEARCH_METHOD")

# Check if account with this search criteria already exists before creating
$BATON_CONNECTOR
EXISTING_ACCOUNTS=$(search_user "$SEARCH_METHOD" "$SEARCH_VALUE")

if [ -n "$EXISTING_ACCOUNTS" ]; then
  echo "Account with $SEARCH_METHOD '$SEARCH_VALUE' already exists. Skipping account creation."
  echo "Existing account ID(s): $EXISTING_ACCOUNTS"
  exit 0
fi

# create account
create_account

# check if account was created
$BATON_CONNECTOR
CREATED_ACCOUNT_IDS=$(search_user "$SEARCH_METHOD" "$SEARCH_VALUE")

# Check if we found any accounts
if [ -z "$CREATED_ACCOUNT_IDS" ]; then
  echo "Failed to create account - no account found with $SEARCH_METHOD '$SEARCH_VALUE'"
  exit 1
fi

# Check if we found multiple accounts (error condition)
if [ -n "$CREATED_ACCOUNT_IDS" ]; then
  ACCOUNT_COUNT=$(echo "$CREATED_ACCOUNT_IDS" | wc -l | tr -d ' ')
  if [ "$ACCOUNT_COUNT" -gt 1 ]; then
    echo "Error: Multiple accounts found with $SEARCH_METHOD '$SEARCH_VALUE'"
    echo "Account IDs found: $CREATED_ACCOUNT_IDS"
    echo "This could lead to incorrect deletion of the wrong user. Aborting."
    exit 1
  fi
fi

# Get the single account ID
CREATED_ACCOUNT_ID=$(echo "$CREATED_ACCOUNT_IDS" | head -n 1)
echo "Account created successfully with ID: $CREATED_ACCOUNT_ID"

if [ -n "$CAPABILITY_CREDENTIAL_ROTATION" ]; then
  # rotate credentials
  $BATON_CONNECTOR --rotate-credentials "$CREATED_ACCOUNT_ID" --rotate-credentials-type "$ACCOUNT_TYPE"
fi

if [ -n "$CAPABILITY_RESOURCE_DELETE" ]; then
  # delete account
  $BATON_CONNECTOR --delete-resource "$CREATED_ACCOUNT_ID" --delete-resource-type "$ACCOUNT_TYPE"

  # delete account already deleted (this should fail gracefully)
  set +e
  $BATON_CONNECTOR --delete-resource "$CREATED_ACCOUNT_ID" --delete-resource-type "$ACCOUNT_TYPE"
  set -e

  # check if account was deleted
  $BATON_CONNECTOR
  REMAINING_ACCOUNTS=$(search_user "$SEARCH_METHOD" "$SEARCH_VALUE")
  
  if [ -n "$REMAINING_ACCOUNTS" ]; then
    echo "Error: Account deletion failed - account with $SEARCH_METHOD '$SEARCH_VALUE' still exists"
    echo "Remaining account ID(s): $REMAINING_ACCOUNTS"
    exit 1
  else
    echo "Account successfully deleted - no account found with $SEARCH_METHOD '$SEARCH_VALUE'"
  fi
fi
