# Connector Account Status Lifecycle Test Action

This action tests disable/enable account status changes for a baton connector that supports these capabilities. This is part of the lifecycle actions category, specifically scoped to account status (enable/disable) functionality.

## Usage

### Basic Usage (Default Configuration)
```yaml
- name: Test Account Status Changes
  uses: ./.github/actions/account-status-lifecycle-test
  with:
    connector: './my-connector'
    account-id: 'user-12345'
```

### Custom Action Names and Parameters
```yaml
- name: Test Account Status Changes
  uses: ./.github/actions/account-status-lifecycle-test
  with:
    connector: './my-connector'
    account-id: 'user-12345'
    enable-action-name: 'enableUser'  # Custom action name
    disable-action-name: 'disableUser'  # Custom action name
    id-parameter-name: 'user-id'  # Custom parameter name (e.g., user-id, accountId, etc.)
```

### Test Only Enable Action
```yaml
- name: Test Enable Only
  uses: ./.github/actions/account-status-lifecycle-test
  with:
    connector: './my-connector'
    account-id: 'user-12345'
    test-flow: 'enable-only'  # Only test enable + verification
```

### Test Only Disable Action
```yaml
- name: Test Disable Only
  uses: ./.github/actions/account-status-lifecycle-test
  with:
    connector: './my-connector'
    account-id: 'user-12345'
    test-flow: 'disable-only'  # Only test disable + verification
```

### Test Enable Then Disable
```yaml
- name: Test Enable Then Disable
  uses: ./.github/actions/account-status-lifecycle-test
  with:
    connector: './my-connector'
    account-id: 'user-12345'
    test-flow: 'enable-disable'  # Enable first, then disable
```

## Inputs

### Required
- `connector`: Connector binary to test
- `account-id`: Account ID to test status changes

### Optional
- `enable-action-name`: Name of the action to enable the account (default: `"enable_user"`)
  - Examples: `"enable_user"`, `"enableUser"`, `"activate_user"`, etc.
- `disable-action-name`: Name of the action to disable the account (default: `"disable_user"`)
  - Examples: `"disable_user"`, `"disableUser"`, `"deactivate_user"`, etc.
- `id-parameter-name`: Parameter name to send the account ID in the action (default: `"user_id"`)
  - Examples: `"user_id"`, `"user-id"`, `"accountId"`, `"id"`, etc.
  - Note: Most connectors use `"user_id"` (snake_case) as the convention
- `test-flow`: Test flow to execute (default: `"disable-enable"`)
  - `"disable-enable"`: Disable the account, then enable it (default)
  - `"enable-disable"`: Enable the account, then disable it
  - `"enable-only"`: Only test enabling the account (with verification)
  - `"disable-only"`: Only test disabling the account (with verification)

## What it tests

The action performs account status tests based on the selected `test-flow`:

### All Test Flows Include:
1. **Initial Status Check**: Gets and displays the initial status of the account
2. **Current Status Verification**: Verifies the current status of the account (enabled or disabled)

### disable-enable (Default)
3. **Disable Account**: Disables the account using the configured disable action
4. **Disable Verification**: Verifies the account is now disabled
5. **Enable Account**: Enables the account using the configured enable action
6. **Enable Verification**: Verifies the account is now enabled

### enable-disable
3. **Enable Account**: Enables the account using the configured enable action
4. **Enable Verification**: Verifies the account is now enabled
5. **Disable Account**: Disables the account using the configured disable action
6. **Disable Verification**: Verifies the account is now disabled

### enable-only
3. **Enable Account**: Enables the account using the configured enable action
4. **Enable Verification**: Verifies the account is now enabled

### disable-only
3. **Disable Account**: Disables the account using the configured disable action
4. **Disable Verification**: Verifies the account is now disabled

## Status Detection

The action checks for account status using the following logic:
- Extracts status from the UserTrait annotation
- Checks if status equals `STATUS_ENABLED` (the only enabled status in baton-sdk)
- Any other status value is considered disabled:
  - `STATUS_DISABLED` - Account is disabled
  - `STATUS_DELETED` - Account is deleted
  - `STATUS_UNSPECIFIED` - Status is not specified
  - `unknown` - Status could not be retrieved

## Customizing for Different Connectors

Different connectors may use different action names and parameter names. This action allows you to configure:

### Action Names
- Some connectors use snake_case: `enable_user`, `disable_user`
- Some connectors use camelCase: `enableUser`, `disableUser`
- Some connectors use different names: `activate_user`, `deactivate_user`, etc.

### Parameter Names
- Most connectors use: `user_id` (snake_case) - this is the default and convention
- Some connectors expect: `user-id` (kebab-case)
- Some connectors expect: `accountId` or `id`

### Example: Custom Connector Configuration
```yaml
- name: Test Custom Connector
  uses: ./.github/actions/account-status-lifecycle-test
  with:
    connector: './custom-connector'
    account-id: 'user-12345'
    enable-action-name: 'activateAccount'
    disable-action-name: 'deactivateAccount'
    id-parameter-name: 'accountId'
    test-flow: 'enable-disable'
```

## When to use

Use this action when you want to test that your connector properly supports:
- Disabling user accounts
- Enabling user accounts
- Verifying status changes are reflected correctly
- Proper status querying and verification
- Different action naming conventions
- Testing only one direction (enable or disable) when needed

This action is separate from other test actions since not all connectors support account status change capabilities. It is specifically scoped to account status lifecycle actions (enable/disable), not all lifecycle actions (which may include update_profile, etc.).

