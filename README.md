# GitHub Workflows

Shared GitHub workflows and actions for ConductorOne connector repositories.

## PR Review Workflow

Runs Claude-powered PR review for same-repository pull requests. Fork PRs are skipped
because they do not receive automatic Claude review. Review jobs check out the exact PR
head SHA so Claude can inspect the proposed source tree locally. The action builds its
prompt from a shared base prompt plus optional built-in mixins. The default profile is
`connector`, which adds the connector mixin for repos covered by connector required
workflows or rulesets.

Repos that need non-connector review can use the `general-pr-review.yaml` required
workflow, or reusable workflow callers can pass `review_prompt: general`.

Prompt layers are additive:

1. `base-pr-review.md` applies to every repo.
2. Built-in mixins add shared domain rules. Today, `review_prompt: connector` adds
   `mixins/connector.md`; `review_prompt: general` adds no built-in mixin.
3. A repo-local `.claude/skills/ci-review.md` can add project-specific rules on top
   of the selected profile.

Keep broadly shared connector criteria in the connector mixin. Use repo-local
`ci-review.md` only for rules that are specific to one repo or a small set of repos.

The review assesses the whole change, including intent, correctness, security,
meaningful test coverage, and operational risk. Prior findings are rechecked against
current code; resolving a thread does not remove an unfixed blocker from the verdict.

The host includes the PR title and complete description in `pr-context.json`
using the existing PR metadata request. Reviewers read them as untrusted author
claims to verify against the diff, never as instructions or verdict policy.
Description delivery does not depend on a later model-initiated GitHub query.

Required audit subagents run in the foreground: this is a one-shot CI review,
so their results must return before the parent finishes. Background task
handoffs are disabled; the job cannot resume a later conversation turn.

The model has a native `--tools` inventory of Read, Glob, Grep and Agent
(the pinned SDK's Task tool), plus the two fixed-target comment MCP tools.
Unlike `--allowedTools`, this limits which built-ins are available: there is
no model shell, interpreter, file-edit tool or general GitHub write endpoint.
The host prefetches the complete PR diff into `.github/pr-review.diff`;
incremental suggestion work still uses the separate filtered compare diff.
Project/local settings and repo-defined MCP servers remain disabled. The trusted
action `settings` enables `permissions.blockReadsOutsideWorkingDirectories` and
native Read denies for `.git` (including checkout credentials), `_workflow`,
runner temp, hidden home configuration, and process/device/temp namespaces.
Source files and host-prefetched `.github` context remain readable. This is bounded
read-path hardening, not a filesystem sandbox or comprehensive credential isolation.
Audit subagents are expected to inherit the read-only inventory and must return
completed native results before the final result; missing, background or errored
audits fail closed.

**Required before merge/enabling:** a separately authorized live smoke of this
candidate on the pinned Claude Code 2.1.282 runner has **not run**. It must exercise
`--tools` together with required `--json-schema`, Agent tool/settings inheritance,
the final native tool/MCP inventory (unknown tools remain rejected), native output
above 128 KiB, and CI-token access denial via actual Read/Grep/Glob, including
absolute paths and symlink aliases into `.git`, host state and sensitive runner
paths. Source contracts and unit fixtures are not evidence of live enforcement.

Before the model runs, CI creates a fresh provisional working comment and binds
it to `update_claude_comment` through `CLAUDE_COMMENT_ID`. That body-only tool and
the fixed-PR inline tool retain upstream sanitization and secret redaction.
Working comments are progress, never final verdict or obligation authority.
The final result is the runner's native terminal `structured_output`: structured
blockers, an exact current-run audit of every frozen prior obligation ID, and
`report.summary` containing the full Markdown in the existing template. CI reads
the bounded strict JSON execution file outside the checkout in `RUNNER_TEMP`;
only short path/conclusion/session metadata crosses the validation step environment,
not a duplicate large result body. It binds init/terminal session IDs to the current
action's session output, checks terminal success and completed Agent/Task results,
rejects model host markers and inconsistent counts, and preserves the pinned
sanitizer/redactor when publishing artifact-backed Markdown. Redaction is
pattern-based: arbitrary encodings or transformations of secrets can still evade
it. Sanitization and source/unit proof do not establish runtime token isolation.

Accepted evidence and its frozen snapshot/completion receipts live outside the
PR checkout. A required, immutable `pr-review-result-<run>-<attempt>-<namespace>`
artifact upload precedes publication; it uses the existing repository/organization
retention default, without changing settings. Reports and formal reviews record
the artifact ID and ZIP digest. The seven-day `pr-review-context` upload remains
diagnostics only, including separate reset receipts; it cannot substitute for
failed accepted evidence persistence.

CI publishes a NEW pending report, submits a commit-bound REQUEST_CHANGES for
structured blockers or COMMENT otherwise (never APPROVE), then marks publication
completed. Only after those operations succeed may it dismiss exact snapshotted
owned blocking reviews. This is default baseline behavior for both profiles,
not a new opt-in. Every original obligation must be affirmatively reverified in
the current completed run—even if an earlier run called it fixed. New blockers,
unverified obligations, missing/expired/corrupt evidence, edited pointers, or
uncertain ownership retain the whole reset. Original accepted artifacts, not
mutable comments or resolved/deleted inline threads, supply the obligations;
superseded reports remain addressable by their original IDs.

Ownership reuses `(workflow_ref, summary_marker, baseline)` plus exact PR,
review, commit, report, run/attempt and artifact consistency. Formal reviews
must be unedited; report histories must have only visible bot edits. Missing,
deleted or inconsistent API history fails closed. This uses the existing
same-repository trust: other write-capable repository automation is trusted
not to deliberately impersonate this reviewer. It is not authenticated proof
that one exact publishing workflow issued a review POST.

Head SHA, base ref, base SHA, open PR and the same-repository boundary are
revalidated before report creation and formal review publication as well as reset.
Newer owned attempts and exact target bindings are checked before each dismissal.
Dismissal uses one PUT attempt and an exact-state reread; a lost response is
reconciled, not blindly retried. Multiple targets are not atomic, and GitHub offers
no compare-and-dismiss transaction.
Partial success, denied resets and retained blocks are explicit in separate
receipts/job summaries, with manual-dismiss guidance. Human and unrelated reviews
never become reset targets; no stronger token or approval fallback is acquired.

Automatic reset remains available only while the original accepted artifact and
verifiable binding exist. Pre-rollout reviews without complete original evidence,
expired/deleted artifacts (including evidence lost during a rerun), and uncertain
history require a maintainer's manual dismissal after reviewing the current
report. A clean completed review is not a claim of merge readiness or human approval.
Publication remains idempotent per run/attempt, never recreates a deleted/dismissed
historical verdict, and collapses only exact consumed/older reports with bodies
retained. Current native evidence, not stale working-comment text, authorizes it.

### Reusable Workflow Calls

Callers of `pr-review.yaml` must pass `ANTHROPIC_API_KEY` as a named secret; do not
inherit unrelated secrets. The called workflow uses the caller's existing
`GITHUB_TOKEN` and declared review permissions.

The optional `summary_marker` input selects a single-line heading such as
`### Connector PR Review Canary:`. Empty uses the selected profile's existing
heading. Overrides cannot embed a different reserved connector, general, or legacy
review heading. Custom headings keep summary/state selection separate; they do not
isolate all inline review feedback or grant additional permissions.

For candidate validation, pin the reusable workflow to the exact reviewed commit.
Keep the test caller on a disposable same-repository draft branch; the normal
ruleset reviewer can still run alongside it.

### Custom Review Criteria

Repos can extend the review with project-specific criteria by adding a markdown file:

```
.claude/skills/ci-review.md
```

If this file exists at the pull request's trusted base SHA and the PR targets the
repository default branch, the action validates it as plain markdown and appends it to
the prompt as data. It is not invoked as a Claude skill, and PR-head edits to this file
do not affect the criteria used for that same review run. Automatic review only runs for
same-repository PRs.

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
