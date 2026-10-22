# GitHub Workflows

Shared GitHub workflows and actions for ConductorOne connector repositories.

## CA Trust Guard

Use the [CA Trust Guard action](.github/actions/ca-trust-guard/README.md) as a
job's first step for offline incident-chain checks, fingerprint-checked OS
denial and a committed clean-bundle environment. No caller checkout is needed.

## Rekor Identity Monitor

`.github/workflows/rekor-monitor.yaml` runs hourly on `main`, watching the three
public release-workflow SANs and the GitHub Actions issuer configured in
`tools/rekor-monitor.yaml`. It installs the upstream monitor at
`v0.0.0-20261019132539-47017b2379cb` through the Go module proxy and checksum
database, with `GOTOOLCHAIN=local` and `GOFLAGS=-mod=readonly`.
The module sum is `h1:+ICdEtSt8tWgvUR8YdwoMOavuNf2yWEqH5FXSpxVdhI=`.
The existing locked-tools action supplies the Go toolchain.

The upstream binary verifies signed checkpoints and consistency with the prior
checkpoint; it does **not** verify matched entries' inclusion. The small isolated
`tools/rekor-entry` Go helper verifies the fetched entry's RFC 6962 inclusion,
body/UUID binding, signed timestamp, checkpoint signature and consistency with
the monitor's verified view. A proof's signed checkpoint may be newer than the
saved view, which is why simply comparing roots in Python would be insufficient.
The helper emits authenticated certificate metadata for hashedrekord entries;
the stdlib-only Python adapter performs the public-data joins.

Fulcio chain checks use committed public roots and intermediates extracted from
the public-good TUF `trusted_root.json` after a Sigstore Go TUF refresh:

| Material | SHA256 |
| --- | --- |
| `tools/rekor-fulcio-roots.pem` (2 roots) | `d543eb6e067c069c6b2fd2c22258452db2f923b221597303a6bce4c664209ee6` |
| `tools/rekor-fulcio-intermediates.pem` (1 intermediate) | `ac5a60ca80f473f8b7742111f9eb49bf7dbd8f03f41a5b39d58b62b6f0de4d0f` |
| Verified `trusted_root.json` source | `6494e21ea73fa7ee769f85f57d5a3e6a08725eae1e38c755fc3517c9e6bc0b66` |

Extraction used TUF snapshot version 166, targets version 14 and root version 16.
Root changes require a reviewed refresh; this does not install a new OS trust
anchor. Non-chaining certificates are skipped by upstream before identity
matching and retained as informational chain-failure diagnostics. The entry
helper likewise reports `UNAUTHENTICATED-SAN`, never a run-identity match.

Each supported entry has separate **run-identity-match** and
**artifact-authorized** verdicts. A real concluded GitHub run, pinned reusable
workflow reference, matching caller commit and plausible signature time prove
the former, not the latter. Certificate repository/tag extensions locate the
public catalog index, connector index and exact release manifest at
`https://dist.conductorone.com`. Known archive/checksum hashes and the hash of
the published manifest bytes are joinable signed-byte digests. Missing runs,
unexpected workflow refs and unrecorded digests on artifact-only releases are
anomalies. In-flight runs and not-yet-published digests stay `PENDING` for at most
six hours; the pending observations persist across cron runs.

`UNKNOWN` is not success or an anomaly: hidden catalogs (including HTTP 403),
unknown storage aliases, missing certificate extensions, DSSE/intoto payloads
and non-joinable signatures are never given invented joins. A hashedrekord OCI
signature records the hash of simple-signing payload bytes, **not** the image
digest. The current manifest has no `signed` flag: the ambiguity budget is the
number of public-ECR `images` entries with `isIndex: true`; private Lambda
images do not count. At most that many distinct unrecorded hashes per release
remain `UNKNOWN` after reconciliation; the next is an anomaly. The budget
persists across runs, counts distinct hashes rather than duplicate log entries,
and must be revisited if the signed-image set or manifest schema changes.

### Initialization, state and alert handoff

After review/merge, dispatch the workflow once on `main` with `bootstrap: true`.
The first checkpoint establishes a baseline, **not** historical coverage.
Subsequent runs restore the newest non-expired `rekor-monitor-state` artifact
from a main-branch run of this workflow (30-day retention), preserving the
checkpoint, pending entries, digest budget and reports. Artifact-less cancelled
or early-failed runs cannot wedge restoration. Each candidate is downloaded to
an isolated temporary directory and accepted only when `checkpoint.txt` and
`reconciliation.json` are both non-empty; incomplete candidates fall through to
older artifacts with a warning. Download failures instead fail closed without
falling back, so an older budget or observation clock cannot be re-published as
the newest state. An always-run completeness check applies the same requirement
before uploading state, so partial first attempts do not perpetuate incomplete
artifacts. Missing or expired usable state fails closed rather than silently
restarting; an operator must recover the last valid artifact before resuming.
Artifacts and job logs remain the evidence; no state is committed by the bot.

Signed checkpoints contain no timestamp. The adapter persists the first
wall-clock observation of each origin/size/root and fails if the same view is
unchanged for more than two hours. Changing only its note signature does not
reset the clock. Initial-view currency is unproven, and a quiet log cannot be
distinguished from a stalled feed at first observation.

Alerts are **not wired**: reports and failure annotations name the test-channel
placeholder `#ops-testing-new-alerts`; no webhook or production routing is
configured. Configure and exercise delivery to that test channel before any
production alerting. No attacker tokens, live signing or self-signed canary
entries are requested by this workflow.

Recorded low-priority follow-ups: add a scheduled dedicated-identity canary to
close the initial-view/liveness gap; compare DSSE payload hashes to payloads in
published provenance/SBOM bundles; schedule reviewed TUF root refreshes; and
index published OCI simple-signing payload hashes. Until the last item, replay
inside an image release's ambiguity budget is undetected. A replay over an
already-recorded digest is inherently indistinguishable. This is a detection
aid over published data, not a replacement for registry signature verification
or proof that a run intended particular bytes.

Offline checks: `python3 scripts/test-rekor-monitor.py`,
`go -C tools/rekor-entry test ./...`, and `make test-scripts`. They use synthetic
fixtures, including replay, missing run, frozen checkpoint, pending/resolved
publication, image-budget exhaustion, altered proofs and untrusted certificates.
Live fleet/axiomatic release observation and alert delivery remain rollout
rehearsals, not claims established by fixture tests.

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

The agent posts its verdict in a working summary comment and never writes
review-state metadata. Before the agent runs, the action creates a fresh
provisional working comment for the run/attempt and binds it to the native
`update_claude_comment` MCP tool (`CLAUDE_COMMENT_ID`), so the agent passes
only the Markdown body and never chooses a repository, comment, or head
target. The body no longer travels through shell heredocs or `gh api` writes;
the tool keeps its upstream sanitization and secret redaction. GitHub's comment
size limit still applies, but the shell's command-length limit does not. The
existing broad `Bash(gh api:*)` permission remains; the prompt restricts its
intended use to metadata reads, not an enforced read-only boundary. This is a
transport fix, not credential isolation. The preflight binds a fresh slot, and
the publisher still validates the live head and report finality at completion.
After a successful run, CI publishes the report as a NEW
comment carrying a visible reviewed-commit link and CI-owned review-state
metadata (reviewed SHA, base, workflow, run, attempt, summary marker, verdict
mode), then submits a commit-bound request-changes review for blockers or a
neutral comment otherwise, linking directly to the report. This reviewer never
approves. Only after the formal review exists does CI mark the report
`publication: completed` — a completed report is the report PLUS its formal
result, so a report whose review never landed stays `pending`: preserved and
reconciled by identity for a same-attempt retry, but never the next run's
review-state baseline and never a comment the agent can edit. Only after
completion are the previous report (including any pending leftover) and the
consumed working comment collapsed — bodies retained, linked to the new
report — so a failed run leaves the previous report untouched.
Stale, provisional, foreign, or malformed working output cannot be published, and a
push during the run stops publication. Working output already consumed by a
published report is never republished without fresh model work, and an attempt
whose start predates an already-completed later attempt (compared by the actual
attempt start times recorded in each report, never run-ID order) is refused as
obsolete before publishing. Publication is idempotent per workflow run
and attempt: a repeated finalization reuses the already-published report and never
submits a second formal review, while an intentional new run or attempt gets a new
report. Completed reports are never handed back to the agent as update targets —
every run/attempt writes to its own freshly created provisional working comment,
while completed state is still selected independently for incremental review.
Active findings appear once in their severity section, labeled `New` or
`Prior — still present`. A compact resolved section records fixed/obsolete prior
findings with evidence; it does not repeat the active findings.

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
   and pin every base image to a reviewed multi-arch index digest
2. Copy the binary from `${TARGETPLATFORM}/<connector-name>`

Example for a Java-based connector:

```dockerfile
FROM gcr.io/distroless/java17-debian13:nonroot@sha256:3b74f9967f1674f49c3b7bd0fc3355e4d8591f8f98b9eed020ff9ca8f5bdb38d
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
