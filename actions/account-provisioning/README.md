# Connector Account Provisioning Test Action

This action tests account provisioning and deprovisioning for a baton connector that supports these capabilities.

## Usage

```yaml
- name: Test Account Provisioning
  uses: ./.github/actions/account-provisioning
  with:
    connector: './my-connector'
    baton-account-login: 'testuser'
    baton-account-email: 'test@example.com'
    baton-account-profile: '{"first_name": "Test", "last_name": "User", "username": "testuser", "email": "test@example.com"}'
    baton-display-name: 'Test User'
    baton-account-type: 'user'
```

## Inputs

### Required
- `connector`: Connector binary to test
- `baton-account-login`: Account login to test provisioning
- `baton-account-email`: Account email to test provisioning
- `baton-account-profile`: Account profile JSON to test provisioning
- `baton-display-name`: Display name for the test account

### Optional
- `baton-account-type`: Type of account to test provisioning (default: 'user')

## What it tests

The action performs a complete account lifecycle test:

1. **Account Creation**: Creates a new account with the specified login, email, and profile
2. **Verification**: Verifies the account was created successfully by checking for its existence
3. **Account Deletion**: Deletes the created account
4. **Delete Again**: Attempts to delete the already-deleted account (tests duplicate deletion handling)
5. **Cleanup Verification**: Verifies the account was completely removed

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
