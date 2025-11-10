# Github Workflows

This repository contains shared GitHub workflows for ConductorOne connector repositories.

## Available Workflows

### Release Workflow

The release workflow handles the release process for connector repos, including:

- Rendering the latest goreleaser and gon configuration files from template
- Building binaries for multiple platforms
- Creating GitHub releases
- Building and pushing Docker images
- Building and pushing ECR images
- Recording releases in the release tracking system

#### Usage

To use the release workflow in your connector repository:

1. Create a `.github/workflows/release.yaml` file with the following content:

```yaml
name: Release

on:
  push:
    tags:
      - "*"

jobs:
  release:
    uses: ConductorOne/github-workflows/.github/workflows/release.yaml@v3
    with:
      tag: ${{ github.ref_name }}
      # defaults to true
      # lambda: false
    secrets:
      RELENG_GITHUB_TOKEN: ${{ secrets.RELENG_GITHUB_TOKEN }}
      APPLE_SIGNING_KEY_P12: ${{ secrets.APPLE_SIGNING_KEY_P12 }}
      APPLE_SIGNING_KEY_P12_PASSWORD: ${{ secrets.APPLE_SIGNING_KEY_P12_PASSWORD }}
      AC_PASSWORD: ${{ secrets.AC_PASSWORD }}
      AC_PROVIDER: ${{ secrets.AC_PROVIDER }}
      DATADOG_API_KEY: ${{ secrets.DATADOG_API_KEY }}
```

2. Ensure your repository has the following secrets configured:

   - `RELENG_GITHUB_TOKEN`: A GitHub token with permissions to create releases
   - `APPLE_SIGNING_KEY_P12`: Base64-encoded Apple signing key
   - `APPLE_SIGNING_KEY_P12_PASSWORD`: Password for the Apple signing key
   - `AC_PASSWORD`: Apple Connect password
   - `AC_PROVIDER`: Apple Connect provider
   - `DATADOG_API_KEY`: Datadog API key for monitoring releases

3. Remove all GoReleaser and gon files from your repository, if they were previously created there.

#### Input Parameters

The release workflow accepts the following input parameters:

| Parameter | Required | Description                      |
| --------- | -------- | -------------------------------- |
| `tag`     | Yes      | The release tag (e.g., "v1.0.0") |

## Available Actions

### Get Baton

The get-baton action downloads the latest version of [Baton](https://github.com/conductorone/baton) and installs it to /usr/local/bin/baton.

#### Usage

```yaml
- name: Install baton
  uses: ConductorOne/github-workflows/actions/get-baton@v3
```

You can then use the baton command in your workflow.

### Sync Test

The sync-test action tests syncing, granting, and revoking for a baton connector.

#### Usage

```yaml
- name: Test Connector Sync
  uses: ConductorOne/github-workflows/actions/sync-test@v3
  with:
    connector: "./my-connector"
    baton-entitlement: "admin-role"
    baton-principal: "user123"
    baton-principal-type: "user"
```

### Account Provisioning Test

The account-provisioning action tests account provisioning and deprovisioning for a baton connector that supports these capabilities.

#### Usage

```yaml
- name: Test Account Provisioning
  uses: ConductorOne/github-workflows/actions/account-provisioning@v2
  with:
    connector: "./my-connector"
    account-email: "test@example.com"
    account-login: "testuser" # optional
    account-display-name: "Test User" # optional
    account-profile: '{"first_name": "Test", "last_name": "User", "username": "testuser", "email": "test@example.com"}' # optional
    account-type: "user" # optional, defaults to 'user'
    search-method: "email" # optional, defaults to 'email'
```

## Development

To modify these workflows:

1. Make your changes in this repository
2. Test the changes in a connector repository _pointing at your branch_
3. Create a pull request for review
4. Once approved, merge to main
5. Tag the release: `git tag v3.0.1`
6. Push the tag: `git push origin v3.0.1`
7. Update the major version tag `git tag -f v3 v3.0.1`
8. Push the major version tag `git push origin v3 --force`

## Versioning

The workflows are versioned using Git tags. When testing a new version of the workflows in your repository, you can specify a specific version:

```yaml
uses: ConductorOne/github-workflows/.github/workflows/release.yaml@my-branch
```

Github does not resolve semantic versioning - tags must match exactly. The major version must _float_.
