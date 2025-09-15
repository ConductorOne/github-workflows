# Connector Account Provisioning Test Action

This action tests account provisioning and deprovisioning for a baton connector that supports these capabilities.

## Usage

### Basic Usage (Email Search - Default)
```yaml
- name: Test Account Provisioning
  uses: ./.github/actions/account-provisioning
  with:
    connector: './my-connector'
    account-email: 'test@example.com'
    account-login: 'testuser'  # optional
    account-profile: '{"first_name": "Test", "last_name": "User", "username": "testuser", "email": "test@example.com"}'  # optional
    account-type: 'user'  # optional, defaults to 'user'
    search-method: 'email'  # optional, defaults to 'email'
```

### Login-based Search
```yaml
- name: Test Account Provisioning with Login Search
  uses: ./.github/actions/account-provisioning
  with:
    connector: './my-connector'
    account-email: 'test@example.com'
    account-login: 'testuser'  # required for login search
    account-type: 'user'
    search-method: 'login'
```

### Display Name-based Search
```yaml
- name: Test Account Provisioning with Display Name Search
  uses: ./.github/actions/account-provisioning
  with:
    connector: './my-connector'
    account-email: 'test@example.com'
    account-display-name: 'Test User'  # required for display_name search
    account-type: 'user'
    search-method: 'display_name'
```

## Inputs

### Required
- `connector`: Connector binary to test
- `account-email`: Account email to test provisioning

### Optional
- `account-login`: Account login to test provisioning
- `account-display-name`: Account display name to test provisioning
- `account-profile`: Account profile JSON to test provisioning
- `account-type`: Type of account to test provisioning (default: 'user')
- `search-method`: Method to search for the created user (default: 'email')
  - `email`: Search by email address (default, backward compatible)
  - `login`: Search by login/username (requires `account-login` to be set)
  - `display_name`: Search by display name (requires `account-display-name` to be set)

## What it tests

The action performs a complete account lifecycle test:

1. **Pre-check**: Checks if an account with the specified search criteria already exists (skips creation if found)
2. **Account Creation**: Creates a new account with the specified email, and optionally login and profile
3. **Verification**: Verifies the account was created successfully by searching for it using the specified method
4. **Safety Check**: Ensures only one account matches the search criteria to prevent incorrect deletions
5. **Account Deletion**: Deletes the created account (if connector supports `CAPABILITY_RESOURCE_DELETE`)
6. **Delete Again**: Attempts to delete the already-deleted account (tests duplicate deletion handling)
7. **Cleanup Verification**: Verifies the account was completely removed by confirming no account matches the search criteria

## Search Methods

### Email Search (Default)
- **Use case**: When the connector API returns email information
- **Search field**: User's email address
- **Requirements**: `account-email` must be provided

### Login Search
- **Use case**: When email is not available but login/username is reliable
- **Search field**: User's login/username
- **Requirements**: `account-login` must be provided

### Display Name Search
- **Use case**: When neither email nor login are reliable, but display name is consistent
- **Search field**: User's display name
- **Requirements**: `account-display-name` must be provided

## Safety Features

- **Multiple Match Detection**: If multiple accounts match the search criteria, the action aborts to prevent incorrect deletion
- **Pre-creation Check**: Skips account creation if an account with the same search criteria already exists
- **Validation**: Ensures required fields are provided based on the selected search method

## Capability Detection

The action automatically detects if the connector supports the required capabilities:
- If `CAPABILITY_ACCOUNT_PROVISIONING` is not found, account provisioning tests are skipped
- If `CAPABILITY_RESOURCE_DELETE` is not found, account deprovisioning tests are skipped

## When to use

Use this action when you want to test that your connector properly supports:
- Creating new user accounts
- Deleting existing user accounts
- Proper cleanup and verification of account operations

This action is separate from the sync-test action since not all connectors support account provisioning capabilities.