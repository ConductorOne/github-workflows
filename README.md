# GitHub Workflows

Shared GitHub workflows and actions for ConductorOne connector repositories.

## PR Review Workflow

Runs Claude-powered PR review for same-repository pull requests. Fork PRs are
skipped because they do not receive automatic Claude review. The action builds its
prompt from a shared base prompt plus optional built-in mixins, and CI — not the
model — publishes the report and submits the formal review. See [detailed
documentation](docs/pr-review.md) for the prompt layers, verdict modes,
publication guarantees, and custom review criteria.

The default profile is `connector`. Repos that need non-connector review can use
the `general-pr-review.yaml` required workflow, or reusable workflow callers can
pass `review_prompt: general`.

### Clearing this reviewer's previous blocks

Both profiles attempt self-dismissal by default, never approval. Before the
model runs, CI freezes exact reviews positively owned by this workflow/profile,
their original count-accounted blocking findings, and ALL typed prior-finding
audit inputs. Known suggestions may remain nonblocking; scraped deduplication
context and fenced prompt bullets do not become reset obligations.
Top-level Security/Correctness list items are counted once, with nested bullets,
wrapped text, and supporting fenced/quoted examples preserved in each audit item.
Capture uses the authoritative frozen input in runner temporary storage; the
workspace copy is model-readable input only. New reset writes replace destination
entries atomically, and preparation clears stale reset snapshots without following
file symlinks. If there are no eligible targets, proof capture/upload are skipped.
A successful terminal response may supply a compact JSON audit
proof for those IDs with complete coverage, no blockers, and affirmative
current-code evidence. It supplements, rather than replaces, the normal review;
CI supplies run/attempt, head/base, and session provenance, not the model proof.
CI captures it from the runner execution file, sanitizes/redacts all persisted
evidence, and uploads one accepted reset artifact using repository retention
defaults. Editable summary prose is not reset authority.

Only after normal publication completes with a neutral COMMENT does CI recheck
the PR head/base, frozen targets, original evidence, and competing/newer runs,
then dismiss those exact blocking reviews. A failed preparation, absent/malformed
proof, incomplete audit, failed upload, stale source, or ambiguous GitHub result
retains blocks without suppressing ordinary publication or changing its state
lifecycle. An incomplete review still keeps its provisional summary as before.
Reset warnings and short redacted per-target retain reasons are visible in the
workflow log; ambiguous writes are reconciled by reading the exact review,
never blindly retried.

Human, foreign-profile, and unmarked legacy reviews are never targets.
Missing, edited-by-human, deleted-history, or count-unaccounted original finding
evidence requires manual handling; this is not a migration of every historical
block.
An incomplete edit/competing-run inventory also retains the affected block
(including more than 100 original edits or GitHub's capped run listing).
No new permissions or caller opt-in are needed. The model retains its existing
tools and write-capable GitHub token: this host ordering is not adversarial
credential isolation and does not replace required human approval.

### Reusable Workflow Calls

Callers of `pr-review.yaml` must pass `ANTHROPIC_API_KEY` as a named secret; do not
inherit unrelated secrets. The called workflow uses the caller's existing
`GITHUB_TOKEN` and declared review permissions.

| Input | Default | Purpose |
| --- | --- | --- |
| `review_prompt` | `connector` | Which built-in mixin to add: `connector` or `general`. |
| `summary_marker` | profile's heading | Override the review summary heading. See [Custom summary headings](docs/pr-review.md#reusable-workflow-calls). |
| `verdict_mode` | `judge` | How CI turns the summary into a formal review. `judge` is granted only where the base branch requires 0 approving reviews, and downgrades to `baseline` otherwise. See [Verdict modes](docs/pr-review.md#verdict-modes). |

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

| Parameter | Required | Default | Description |
|-|-|-|-|
| `tag` | Yes | - | The release tag (must be valid semver with `v` prefix, e.g., `v1.0.0`) |
| `release_storage_name` | No | `""` | Optional S3 release path segment matching `^[a-z][a-z0-9-]{0,99}$`; defaults to the repository name |
| `go_main_package` | No | `""` | Relative Go main package. Defaults to `./cmd/<repository-name>`; use `./` for a root command package. |
| `brew_tap` | No | `homebrew-baton` | ConductorOne Homebrew tap repository name. Must not contain a path separator. |
| `go_source_hygiene` | No | `false` | Run `go generate ./...` and `go mod tidy`; fail if either changes the tagged source tree. |
| `go_vulnerability_scan` | No | `false` | Run `govulncheck` for Linux, macOS, and Windows before release. |
| `verify_module_version` | No | `false` | Require a generated binary to embed the release tag as its Go module version. |
| `lambda` | No | `true` | Whether to release with Lambda image support |
| `docker` | No | `true` | Whether to release with Docker image support |
| `dockerfile_template` | No | `""` | Path to a custom Dockerfile in your repo (only valid when `lambda: false`) |
| `docker_extra_files` | No | `""` | Comma-separated list of extra files/dirs to include in Docker build context |
| `msi` | No | `true` | Whether to build MSI Windows installers |
| `msi_wxs_path` | No | `""` | Path to custom WXS template for MSI installer (uses default if not set) |
| `windows_pre_build_script` | No | `""` | Optional safe repo-relative Bash script, run in the caller checkout by GoReleaser before Windows compilation with `RELEASE_TAG` set to the requested tag. |
| `brew` | No | `true` | Whether to publish a Homebrew formula to the selected public `conductorone` tap |

2. Ensure your repository has the following secrets configured:

   - `RELENG_GITHUB_TOKEN`: A GitHub token with permissions to create releases
   - `APPLE_SIGNING_KEY_P12`: Base64-encoded Apple signing key
   - `APPLE_SIGNING_KEY_P12_PASSWORD`: Password for the Apple signing key
   - `AC_PASSWORD`: Apple Connect password
   - `AC_PROVIDER`: Apple Connect provider
   - `DATADOG_API_KEY`: Datadog API key for monitoring releases
   - `GORELEASER_PRO_KEY`: GoReleaser Pro license key (required when `msi: true`, the default)

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
FROM gcr.io/distroless/java17-debian13:nonroot
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

To disable MSI builds entirely (e.g., for connectors that don't need Windows installers):

```yaml
    with:
      tag: ${{ github.ref_name }}
      msi: false
```

When `msi: false`, the `GORELEASER_PRO_KEY` secret is not required.

### Windows Pre-build Script

Connectors that generate Windows-specific resources from the release tag can opt in:

```yaml
    with:
      tag: ${{ github.ref_name }}
      windows_pre_build_script: scripts/release-windows-resources.sh
```

The script must exist as a file in the tagged caller repository. Paths must use
only letters, digits, `.`, `_`, `/`, and `-`, and must not be absolute or contain
`..` or whitespace. GoReleaser runs `bash ./<path>` in `_caller` as a
`before.hooks` command, after its normal clean-tree/tag validation and before
Windows compilation. The hook receives the validated workflow tag as
`RELEASE_TAG`; a failing hook fails the Windows release.

This is Windows-only and opt-in: an empty input leaves the rendered Windows
config unchanged and does not run any generation. macOS, Linux, and Docker
builds do not run this hook. Keep connector-specific generation in the caller's
script; do not skip release validation, use snapshots, retag, or commit generated
resources during release.

## Verify Workflow

Runs linting, tests, and optional regression verification. See [detailed documentation](docs/verify-workflow.md) for jobs, regression testing, and all options.

### Usage

```yaml
name: Verify

on:
  pull_request:
    types: [opened, reopened, synchronize]
  push:
    branches:
      - main

jobs:
  verify:
    uses: ConductorOne/github-workflows/.github/workflows/verify.yaml@v4
    with:
      ref: ${{ github.event.pull_request.head.sha || github.sha }}
      connector: baton-okta  # optional: enables regression testing
    secrets:
      RELENG_GITHUB_TOKEN: ${{ secrets.RELENG_GITHUB_TOKEN }}
```

| Parameter | Required | Default | Description |
|-|-|-|-|
| `ref` | Yes | - | Git ref to check out and verify |
| `run_tests` | No | `true` | Run `go test` |
| `connector` | No | `""` | Connector name — triggers [regression testing](docs/verify-workflow.md#regression) when set |

## Connector Docs Verify Workflow

Runs a standalone `docs/connector.mdx` validation check for connector repositories. See [detailed documentation](docs/connector-docs-verify.md) for behavior and rollout notes.

The workflow is safe to require on every connector pull request because it always reports a status. It skips validation when `docs/connector.mdx` is unchanged, fails if that file was changed and removed, and validates MDX safety when the file changed.

The reusable workflow checks out its own validator at the exact workflow commit, installs locked dependencies with npm lifecycle scripts disabled, and checks out caller code without persisted credentials.

### Usage

```yaml
name: Connector Docs

on:
  pull_request:
    types: [opened, reopened, synchronize]
  push:
    branches:
      - main

jobs:
  connector-docs:
    uses: ConductorOne/github-workflows/.github/workflows/connector-docs-verify.yaml@v4
    with:
      ref: ${{ github.event.pull_request.head.sha || github.sha }}
```

When used with the job id `connector-docs`, the required status check context is `connector-docs / validate`.

Roll out the caller workflow before marking the check required. The shared workflow ref, such as `v4`, must include `connector-docs-verify.yaml` before connector repos call it.

## Available Actions

### Get Baton

The get-baton action downloads the latest version of [Baton](https://github.com/conductorone/baton-sdk) and installs it to /usr/local/bin/baton.

```yaml
- name: Install baton
  uses: ConductorOne/github-workflows/actions/get-baton@v4
```

You can then use the baton command in your workflow.

### Sync Test

The sync-test action tests syncing, granting, and revoking for a baton connector.

Before the grant/revoke cycle it also syncs once with deliberately invalid credentials and asserts the connector exits with gRPC `Unauthenticated` (16) or `PermissionDenied` (7). That mapping requires `exit.LogExit` from baton-sdk v0.25.0+; connectors that still `os.Exit(1)` skip this check. Dummy values default to `invalid` for credential-like `BATON_*` env vars (tokens, passwords, secrets, keys). Override with `bad-credentials` when the value must parse as JWT, PEM, JSON, or similar before the connector will attempt authentication.

```yaml
- name: Test Connector Sync
  uses: ConductorOne/github-workflows/actions/sync-test@v4
  with:
    connector: "./my-connector"
    baton-entitlement: "admin-role"
    baton-principal: "user123"
    baton-principal-type: "user"
    sleep: 2 # optional, wait 2 seconds after each write operation
    # optional, only needed when dummy credentials must be well-formed:
    # bad-credentials: |
    #   BATON_PRIVATE_KEY=-----BEGIN PRIVATE KEY----- invalid -----END PRIVATE KEY-----
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
