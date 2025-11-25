#!/bin/bash

set -eo pipefail

if [ -z "$BATON_CONNECTOR" ]; then
  echo "BATON_CONNECTOR not set"
  exit 1
fi

if [ -z "$CONNECTOR_ACCOUNT_ID_STATUS" ]; then
  echo "CONNECTOR_ACCOUNT_ID_STATUS not set"
  exit 1
fi

if ! command -v baton &> /dev/null; then
  echo "baton not found"
  exit 1
fi

# Set defaults
ENABLE_ACTION_NAME="${ENABLE_ACTION_NAME:-enable_user}"
DISABLE_ACTION_NAME="${DISABLE_ACTION_NAME:-disable_user}"
ID_PARAMETER_NAME="${ID_PARAMETER_NAME:-user_id}"
TEST_FLOW="${TEST_FLOW:-disable-enable}"

# Validate test flow
case "$TEST_FLOW" in
  "disable-enable"|"enable-disable"|"enable-only"|"disable-only")
    ;;
  *)
    echo "Error: Invalid test-flow '$TEST_FLOW'. Must be one of: disable-enable, enable-disable, enable-only, disable-only"
    exit 1
    ;;
esac

# Error on unbound variables now that we've set BATON
set -u

# Function to get user status (enabled/disabled)
get_user_status() {
  local user_id="$1"
  local status=$(baton resources -t "user" --output-format=json \
    | jq -r --arg user_id "$user_id" \
         '.resources[] |
          select(.resource.id.resource == $user_id) |
          (.resource.annotations[]? | select(."@type" == "type.googleapis.com/c1.connector.v2.UserTrait")) as $trait |
          if $trait != null and $trait.status != null then
            $trait.status
          else
            "unknown"
          end')
  # Extract just the status value if it's JSON, otherwise return as-is
  echo "$status" | jq -r '.status // empty' 2>/dev/null || echo "$status"
}

# Function to check if user is enabled
is_user_enabled() {
  local user_id="$1"
  local status=$(get_user_status "$user_id")
  # Based on baton-sdk UserTrait_Status_Status enum: STATUS_ENABLED is the only enabled state
  if [ "$status" = "STATUS_ENABLED" ]; then
    return 0  # User is enabled
  else
    return 1  # User is disabled (STATUS_DISABLED, STATUS_DELETED, STATUS_UNSPECIFIED, or unknown)
  fi
}

# Function to enable user
enable_user() {
  local user_id="$1"
  echo "Enabling user with ID: $user_id"
  echo "Using action: $ENABLE_ACTION_NAME with parameter: $ID_PARAMETER_NAME"
  
  # Build the JSON arguments with the configured parameter name
  local json_args=$(jq -n --arg id "$user_id" --arg param "$ID_PARAMETER_NAME" "{(\$param): \$id}")
  
  local result=$($BATON_CONNECTOR --invoke-action="$ENABLE_ACTION_NAME" \
    --invoke-action-args="$json_args" 2>&1)
  
  if [ $? -eq 0 ]; then
    echo "Enable user action completed successfully"
    echo "Result: $result"
    return 0
  else
    echo "Enable user action failed"
    echo "Error: $result"
    return 1
  fi
}

# Function to disable user
disable_user() {
  local user_id="$1"
  echo "Disabling user with ID: $user_id"
  echo "Using action: $DISABLE_ACTION_NAME with parameter: $ID_PARAMETER_NAME"
  
  # Build the JSON arguments with the configured parameter name
  local json_args=$(jq -n --arg id "$user_id" --arg param "$ID_PARAMETER_NAME" "{(\$param): \$id}")
  
  local result=$($BATON_CONNECTOR --invoke-action="$DISABLE_ACTION_NAME" \
    --invoke-action-args="$json_args" 2>&1)
  
  if [ $? -eq 0 ]; then
    echo "Disable user action completed successfully"
    echo "Result: $result"
    return 0
  else
    echo "Disable user action failed"
    echo "Error: $result"
    return 1
  fi
}

# Function to verify user status
verify_user_status() {
  local user_id="$1"
  local expected_status="$2"  # "ENABLED" or "DISABLED"
  
  echo "Verifying user $user_id is $expected_status..."
  
  # Sync to get latest data
  $BATON_CONNECTOR
  
  local actual_status=$(get_user_status "$user_id")
  echo "User $user_id current status: $actual_status"
  
  if [ "$expected_status" = "ENABLED" ]; then
    if is_user_enabled "$user_id"; then
      echo "✓ User is enabled as expected"
      return 0
    else
      echo "✗ User is not enabled as expected"
      return 1
    fi
  elif [ "$expected_status" = "DISABLED" ]; then
    if ! is_user_enabled "$user_id"; then
      echo "✓ User is disabled as expected"
      return 0
    else
      echo "✗ User is not disabled as expected"
      return 1
    fi
  else
    echo "Error: Invalid expected status '$expected_status'. Must be 'ENABLED' or 'DISABLED'"
    return 1
  fi
}

# Main test execution
echo "Starting account status tests..."
echo "Test flow: $TEST_FLOW"
echo "Enable action: $ENABLE_ACTION_NAME"
echo "Disable action: $DISABLE_ACTION_NAME"
echo "ID parameter: $ID_PARAMETER_NAME"

echo "Testing user with ID: $CONNECTOR_ACCOUNT_ID_STATUS"

# Get initial status
echo "Getting initial user status..."
$BATON_CONNECTOR
INITIAL_STATUS=$(get_user_status "$CONNECTOR_ACCOUNT_ID_STATUS")
echo "Initial user status: $INITIAL_STATUS"

# Test 1: Verify current status
echo ""
echo "=== Test 1: Verify current user status ==="
if is_user_enabled "$CONNECTOR_ACCOUNT_ID_STATUS"; then
  echo "User is currently enabled"
else
  echo "User is currently disabled"
fi

# Execute test flow based on TEST_FLOW
case "$TEST_FLOW" in
  "disable-enable")
    # Test 2: Disable user
    echo ""
    echo "=== Test 2: Disable user ==="
    if ! disable_user "$CONNECTOR_ACCOUNT_ID_STATUS"; then
      echo "Failed to disable user"
      exit 1
    fi

    # Verify user is disabled
    if ! verify_user_status "$CONNECTOR_ACCOUNT_ID_STATUS" "DISABLED"; then
      echo "User disable verification failed"
      exit 1
    fi

    # Test 3: Verify current status
    echo ""
    echo "=== Test 3: Verify current user status ==="
    if is_user_enabled "$CONNECTOR_ACCOUNT_ID_STATUS"; then
      echo "User is currently enabled"
    else
      echo "User is currently disabled"
    fi

    # Test 4: Enable user
    echo ""
    echo "=== Test 4: Enable user ==="
    if ! enable_user "$CONNECTOR_ACCOUNT_ID_STATUS"; then
      echo "Failed to enable user"
      exit 1
    fi

    # Verify user is enabled
    if ! verify_user_status "$CONNECTOR_ACCOUNT_ID_STATUS" "ENABLED"; then
      echo "User enable verification failed"
      exit 1
    fi
    ;;

  "enable-disable")
    # Test 2: Enable user
    echo ""
    echo "=== Test 2: Enable user ==="
    if ! enable_user "$CONNECTOR_ACCOUNT_ID_STATUS"; then
      echo "Failed to enable user"
      exit 1
    fi

    # Verify user is enabled
    if ! verify_user_status "$CONNECTOR_ACCOUNT_ID_STATUS" "ENABLED"; then
      echo "User enable verification failed"
      exit 1
    fi

    # Test 3: Verify current status
    echo ""
    echo "=== Test 3: Verify current user status ==="
    if is_user_enabled "$CONNECTOR_ACCOUNT_ID_STATUS"; then
      echo "User is currently enabled"
    else
      echo "User is currently disabled"
    fi

    # Test 4: Disable user
    echo ""
    echo "=== Test 4: Disable user ==="
    if ! disable_user "$CONNECTOR_ACCOUNT_ID_STATUS"; then
      echo "Failed to disable user"
      exit 1
    fi

    # Verify user is disabled
    if ! verify_user_status "$CONNECTOR_ACCOUNT_ID_STATUS" "DISABLED"; then
      echo "User disable verification failed"
      exit 1
    fi
    ;;

  "enable-only")
    # Test 2: Enable user
    echo ""
    echo "=== Test 2: Enable user ==="
    if ! enable_user "$CONNECTOR_ACCOUNT_ID_STATUS"; then
      echo "Failed to enable user"
      exit 1
    fi

    # Verify user is enabled
    if ! verify_user_status "$CONNECTOR_ACCOUNT_ID_STATUS" "ENABLED"; then
      echo "User enable verification failed"
      exit 1
    fi
    ;;

  "disable-only")
    # Test 2: Disable user
    echo ""
    echo "=== Test 2: Disable user ==="
    if ! disable_user "$CONNECTOR_ACCOUNT_ID_STATUS"; then
      echo "Failed to disable user"
      exit 1
    fi

    # Verify user is disabled
    if ! verify_user_status "$CONNECTOR_ACCOUNT_ID_STATUS" "DISABLED"; then
      echo "User disable verification failed"
      exit 1
    fi
    ;;
esac

# Final status check
echo ""
echo "=== Final Status Check ==="
FINAL_STATUS=$(get_user_status "$CONNECTOR_ACCOUNT_ID_STATUS")
echo "Final user status: $FINAL_STATUS"

echo ""
echo "✓ All account status tests completed successfully!"

# Print summary based on test flow
case "$TEST_FLOW" in
  "disable-enable")
    echo "User $CONNECTOR_ACCOUNT_ID_STATUS status changes: $INITIAL_STATUS -> STATUS_DISABLED -> STATUS_ENABLED"
    ;;
  "enable-disable")
    echo "User $CONNECTOR_ACCOUNT_ID_STATUS status changes: $INITIAL_STATUS -> STATUS_ENABLED -> STATUS_DISABLED"
    ;;
  "enable-only")
    echo "User $CONNECTOR_ACCOUNT_ID_STATUS status changes: $INITIAL_STATUS -> STATUS_ENABLED"
    ;;
  "disable-only")
    echo "User $CONNECTOR_ACCOUNT_ID_STATUS status changes: $INITIAL_STATUS -> STATUS_DISABLED"
    ;;
esac

