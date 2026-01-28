# GitHub Workflows

Shared GitHub workflows and actions for ConductorOne connector repositories.

## Release Workflow

Handles building, signing, and publishing connector releases. See [detailed documentation](docs/release-workflow.md) for security properties and internals.

### Usage

1. Create a `.github/workflows/release.yaml` file with the following content:

```yaml
name: Release

on:
  push:
    tags:
      - "*"

jobs:
  release:
    uses: ConductorOne/github-workflows/.github/workflows/release.yaml@v4
    with:
      tag: ${{ github.ref_name }}
    secrets:
      RELENG_GITHUB_TOKEN: ${{ secrets.RELENG_GITHUB_TOKEN }}
      APPLE_SIGNING_KEY_P12: ${{ secrets.APPLE_SIGNING_KEY_P12 }}
      APPLE_SIGNING_KEY_P12_PASSWORD: ${{ secrets.APPLE_SIGNING_KEY_P12_PASSWORD }}
      AC_PASSWORD: ${{ secrets.AC_PASSWORD }}
      AC_PROVIDER: ${{ secrets.AC_PROVIDER }}
      DATADOG_API_KEY: ${{ secrets.DATADOG_API_KEY }}
      GORELEASER_PRO_KEY: ${{ secrets.GORELEASER_PRO_KEY }}
```

The release workflow accepts the following input parameters:

| Parameter             | Required | Default | Description                                                                 |
| --------------------- | -------- | ------- | --------------------------------------------------------------------------- |
| `tag`                 | Yes      | -       | The release tag (must be valid semver with `v` prefix, e.g., `v1.0.0`)      |
| `lambda`              | No       | `true`  | Whether to release with Lambda image support                                |
| `docker`              | No       | `true`  | Whether to release with Docker image support                                |
| `dockerfile_template` | No       | `""`    | Path to a custom Dockerfile in your repo (only valid when `lambda: false`)  |
| `docker_extra_files`  | No       | `""`    | Comma-separated list of extra files/dirs to include in Docker build context |
| `msi_wxs_path`        | No       | `""`    | Path to custom WXS template for MSI installer (uses default if not set)     |

2. Ensure your repository has the following secrets configured:

   - `RELENG_GITHUB_TOKEN`: A GitHub token with permissions to create releases
   - `APPLE_SIGNING_KEY_P12`: Base64-encoded Apple signing key
   - `APPLE_SIGNING_KEY_P12_PASSWORD`: Password for the Apple signing key
   - `AC_PASSWORD`: Apple Connect password
   - `AC_PROVIDER`: Apple Connect provider
   - `DATADOG_API_KEY`: Datadog API key for monitoring releases
   - `GORELEASER_PRO_KEY`: GoReleaser Pro license key (for MSI builds)

3. Remove all GoReleaser, gon files, Dockerfile, and Dockerfile.lambda files from your connector repository, if they were previously created there.

### Custom Dockerfiles

For connectors that require a non-standard base image (e.g., Java runtime), you can provide a custom Dockerfile:

```yaml
jobs:
  release:
    uses: ConductorOne/github-workflows/.github/workflows/release.yaml@v4
    with:
      tag: ${{ github.ref_name }}
      lambda: false
      dockerfile_template: Dockerfile
      docker_extra_files: java # Include the java/ directory in the build context
    secrets:
      # ... secrets ...
```

Your custom Dockerfile must:

1. Use `ARG TARGETPLATFORM` for multi-arch build support
2. Copy the binary from `${TARGETPLATFORM}/<connector-name>`

Example for a Java-based connector:

```dockerfile
FROM gcr.io/distroless/java17-debian11:nonroot
ARG TARGETPLATFORM
ENTRYPOINT ["/baton-example"]

COPY ${TARGETPLATFORM}/baton-example /baton-example
COPY java /java
```

The workflow substitutes `${REPO_NAME}` in your Dockerfile if present, so you can also use:

```dockerfile
COPY ${TARGETPLATFORM}/${REPO_NAME} /${REPO_NAME}
```

**Note:** Use `docker_extra_files` to include additional files or directories (comma-separated) in the Docker build context. These are paths relative to your connector repository root.

### Custom MSI Installers

By default, the workflow builds a simple MSI installer that:
- Installs the binary to `C:\Program Files\ConductorOne\<connector-name>`
- Adds the installation directory to the system PATH

For connectors that require custom MSI behavior (Windows Service, registry keys, etc.), provide a custom WXS template:

```yaml
jobs:
  release:
    uses: ConductorOne/github-workflows/.github/workflows/release.yaml@v4
    with:
      tag: ${{ github.ref_name }}
      msi_wxs_path: ci/app.wxs
    secrets:
      # ... secrets ...
```

Your custom WXS template can use GoReleaser template variables:
- `{{ .ProjectName }}` - Connector name (e.g., "baton-okta")
- `{{ .Binary }}` - Binary name without extension
- `{{ .Version }}` - Full version string
- `{{ .Major }}`, `{{ .Minor }}`, `{{ .Patch }}` - Version components

The `${UPGRADE_CODE}` placeholder is automatically replaced with a deterministic UUID v5 generated from the repository name, ensuring consistent upgrade behavior across versions.

See [baton-runner/ci/app.wxs](https://github.com/ConductorOne/baton-runner/blob/main/ci/app.wxs) for an example Windows Service installer.

## Available Actions

### Get Baton

The get-baton action downloads the latest version of [Baton](https://github.com/conductorone/baton) and installs it to /usr/local/bin/baton.

```yaml
- name: Install baton
  uses: ConductorOne/github-workflows/actions/get-baton@v4
```

You can then use the baton command in your workflow.

### Sync Test

The sync-test action tests syncing, granting, and revoking for a baton connector.

```yaml
- name: Test Connector Sync
  uses: ConductorOne/github-workflows/actions/sync-test@v4
  with:
    connector: "./my-connector"
    baton-entitlement: "admin-role"
    baton-principal: "user123"
    baton-principal-type: "user"
    sleep: 2 # optional, wait 2 seconds after each write operation
```

### Account Provisioning Test

The account-provisioning action tests account provisioning and deprovisioning for a baton connector that supports these capabilities.

```yaml
- name: Test Account Provisioning
  uses: ConductorOne/github-workflows/actions/account-provisioning@v4
  with:
    connector: "./my-connector"
    account-email: "test@example.com"
    account-login: "testuser" # optional
    account-display-name: "Test User" # optional
    account-profile: '{"first_name": "Test", "last_name": "User", "username": "testuser", "email": "test@example.com"}' # optional
    account-type: "user" # optional, defaults to 'user'
    search-method: "email" # optional, defaults to 'email'
    sleep: 2 # optional, wait 2 seconds after each write operation
```

### Account Status Lifecycle Test

The account-status-lifecycle-test action tests disabling and enabling account status changes for a baton connector.

#### Usage

```yaml
- name: Test Account Status Changes
  uses: ConductorOne/github-workflows/actions/account-status-lifecycle-test@v3
  with:
    connector: "./my-connector"
    account-id: "user-12345"
    enable-action-name: "enable_user" # optional, defaults to 'enable_user'
    disable-action-name: "disable_user" # optional, defaults to 'disable_user'
    id-parameter-name: "user_id" # optional, defaults to 'user_id'
    test-flow: "disable-enable" # optional, defaults to 'disable-enable'
    sleep: 2 # optional, wait 2 seconds after each write operation
```

The `test-flow` parameter can be:

- `disable-enable`: Disable the account, then enable it (default)
- `enable-disable`: Enable the account, then disable it
- `enable-only`: Only test enabling the account
- `disable-only`: Only test disabling the account

## Development

See [release-workflow.md](docs/release-workflow.md) for testing and modification guidance.

### Versioning

Workflows are versioned using Git tags. The major version tag (e.g., `v4`) must float:

```bash
git tag v4.0.1 && git push origin v4.0.1
git tag -f v4 v4.0.1 && git push origin v4 --force
```

To test changes, point a connector at your branch:

```yaml
uses: ConductorOne/github-workflows/.github/workflows/release.yaml@my-branch
```
