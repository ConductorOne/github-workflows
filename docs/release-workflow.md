# Release Workflow

The `release.yaml` workflow handles the complete release process for connector repositories, including building, signing, attestation, and publishing.

## Overview

![Release Workflow](diagrams/release-workflow.png)

When a tag is pushed to a connector repository, the shared release workflow:

1. Builds binaries for macOS and Linux (with Apple codesigning)
2. Builds Windows zip and MSI installer (with WiX Toolset)
3. Builds multi-arch Docker images
4. Signs all artifacts with Sigstore (keyless)
5. Generates SLSA provenance attestations
6. Publishes to S3 and ECR Public
7. Records the release in the connector registry API

## Jobs

### validate-inputs

Validates workflow inputs before proceeding:

- Ensures tag is valid semver starting with 'v' (e.g., `v1.2.3`)
- Ensures `release_storage_name`, when set, matches
  `^[a-z][a-z0-9-]{0,99}$`
- Ensures `dockerfile_template` is only used when `lambda: false`
- Ensures `docker_extra_files` is only used when `dockerfile_template` is set
- Ensures `GORELEASER_PRO_KEY` is provided when `msi: true`

### determine-workflows-ref

Resolves the exact SHA of the shared workflow, then uses the pinned workflow source to normalize `go_main_package` and `brew_tap`. An omitted main package becomes `./cmd/<repository-name>`; a root package uses `./`. The workflow rejects non-relative package paths and tap values containing a path separator. The pinned reference is embedded in all provenance attestations, ensuring verifiability.

Before option normalization, the pinned checkout validates `dockerfile_template`,
`msi_wxs_path`, `windows_pre_build_script`, and trimmed `docker_extra_files`
entries through `scripts/validate-safe-relative-path.sh`. The helper has the
single safe-path predicate; callers retain their existing diagnostics and
empty-input guards.
These guarded checks run after the existing pinned checkout rather than adding
an unpinned fetch or permissions to `validate-inputs`; all builds already depend
on `determine-workflows-ref`. No job dependencies or permissions change.

### release-preflight

Runs opted-in source integrity checks against the exact tagged caller source before any build job:

- `go_source_hygiene` runs `go generate ./...` and `go mod tidy`, then rejects a changed tree.
- `go_vulnerability_scan` runs `govulncheck` for Linux, macOS, and Windows.

`verify_module_version` checks a generated binary after GoReleaser and before provenance or artifact upload. It requires the embedded Go module version to equal the release tag.

### goreleaser-binaries (macOS)

Builds and signs binary archives for macOS and Linux:

- Cross-compiles for darwin/linux (amd64/arm64)
- Apple codesigning via gon (macOS only)
- Generates SBOMs using Syft
- Creates SLSA v1 provenance attestations
- Signs SBOMs as attestation bundles
- Uploads all artifacts to S3 with no-overwrite writes
- Writes the Homebrew formula GoReleaser rendered to the `conductorone` tap
  (`scripts/publish-homebrew-formula.sh`), re-reading and retrying when a
  concurrent release moved the tap's branch; a formula already at the same
  content is left alone

**Outputs:** `*.zip` (macOS), `*.tar.gz` (Linux), `*.provenance.sigstore.json`, `*.sbom.sigstore.json`

### goreleaser-windows (Windows)

Builds Windows zip and MSI installer:

- Native Windows build on Windows runner (not cross-compiled)
- Produces both `.zip` archive and `.msi` installer
- MSI built using WiX Toolset with GoReleaser Pro
- Deterministic UpgradeCode via UUID v5 from repository name
- Supports custom WXS templates via `msi_wxs_path` input
- Supports an opt-in caller Bash script via `windows_pre_build_script`
- Generates SBOMs and SLSA v1 provenance attestations
- Uploads all artifacts to S3 with no-overwrite writes

**Outputs:** `*.zip`, `*.msi`, `*.provenance.sigstore.json`, `*.sbom.sigstore.json`

**Custom WXS:** For connectors requiring custom MSI behavior (Windows Service, registry keys, etc.), provide a custom WXS template via `msi_wxs_path`. If not provided, uses the default CLI installer template.

**Windows pre-build script:** `windows_pre_build_script` defaults to empty,
preserving the original rendered GoReleaser config without a hook. When set,
Windows config generation verifies that the safe repo-relative path exists as a
file in `_caller`. The config adds a `before.hooks` command, `bash ./<path>`,
which GoReleaser runs in that same tagged caller checkout after its normal
clean-tree/tag validation and before compilation. Only the Windows GoReleaser
step forwards `RELEASE_TAG` from the validated `tag` input to the hook. Hook
failures fail the release; generated resources remain local to the Windows
build workspace. The workflow does not run this hook on macOS, Linux, Docker,
or callers with the default empty input, and does not skip validation, build
snapshots, retag, or commit generated resources.

### goreleaser-docker (Linux)

Builds and publishes container images:

- Multi-arch Docker images (amd64/arm64)
- Pushes a candidate image to ECR Public
- Promotes the candidate digest to the version tag after preflight
- Updates `latest` as mutable convenience metadata
- Attaches provenance attestations to images (OCI referrers)

**Outputs:** ECR Public images with attached attestations

### publish-release-manifest

Finalizes distributable release artifacts:

- Creates unified checksums file (all platforms)
- Merges binary, Windows, and image manifests
- Signs `manifest.json` and checksums with Sigstore
- Uploads manifest and checksums to S3 with no-overwrite writes
- Exposes the final manifest to the registry API recording job

### record-registry-api

Records release metadata in the connector registry API:

- Reuses the exact manifest uploaded to S3
- Includes documentation and changelog data when present; commit entries that
  repeat a subject within a heading section (routine automation sweeps) are
  folded into one line with a count before the changelog is recorded
- Includes `config_schema.json` and `baton_capabilities.json` when present
- Sends release timestamp, commit SHA, and workflow run metadata
- Retries the record up to five times with doubling waits (10s to 80s) on
  transport errors, 5xx responses, 429, or a 400 whose message reports an
  upstream 5xx from the CDN; other 4xx responses fail immediately

### verify-release

Post-release validation (non-blocking):

- Validates all artifacts are accessible
- Verifies all attestations with cosign
- Logs in to Public ECR with the pull-only `GHA-PublicECRPull` role when the
  release includes a Docker image, so the image attestation pull is not
  subject to the anonymous rate limit
- Triggers Datadog notification on failure

## Security Properties

### Pinned Container Images

The default Dockerfile templates select immutable multi-architecture base-image
indexes. Buildx selects the target platform from each index; the Lambda build
continues to select Linux arm64. `tools/image-locks.txt` records each image
reference, index digest, and UTC resolution date. The helper-image locks are
consumed by the companion release-workflow change that pins QEMU and the
docker-container BuildKit driver.
The companion release-workflow change is now merged, so the helper-image locks
and release-workflow/setup-script Renovate managers are active. Tool-version
proposal managers read `.github/actions/locked-tools/tool-locks.txt`.

Resolved on **2026-10-21** with `crane digest <ref>`:

| Image reference / lock line or distroless platform | Index / platform manifest digest |
| --- | --- |
| `gcr.io/distroless/static-debian13:nonroot` | `sha256:d62067879de890a1def9da0e7766b7d20810a5e5555cef8506b752437d7a3e88` |
| ↳ `linux/amd64` | `sha256:d4df213e27178b79e05ee2bdf8a23d8c54baab3b5a0dc083b039ad9929fa4219` |
| ↳ `linux/arm64/v8` | `sha256:8a198f3cf5e9b249577c18010ec18382a3e9f94a6e5676d57b1ff8438ae113e9` |
| ↳ `linux/arm/v7` | `sha256:38489751a0d2feecbe3b7a840d979e6c4db380558f15c61ee025f378b84b2cf3` |
| ↳ `linux/s390x` | `sha256:c63ef2a68c5ffa9bd31feaa573d606208c10d54b1bee2caf3c168dc5ddec78d9` |
| ↳ `linux/ppc64le` | `sha256:be4a6d22e53c1118d0c026dc08be71ec3f1954ae75f6e5ff4a10ce97b837ff80` |
| ↳ `linux/riscv64` | `sha256:0b160fd367891825201f06378982d59900bf35992cfcc453daf7449892d91226` |
| `public.ecr.aws/lambda/provided:al2023` | `sha256:49c8b85e353ea6052759c3dcf6761b35c055c29c8016a9cd734ac4d24a060729` |
| `tonistiigi/binfmt:latest` | `sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0` |
| `moby/buildkit:buildx-stable-1` | `sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea` |

Distroless intentionally sets image-config creation times to the Unix epoch.
Its registry's [manifest metadata](https://gcr.io/v2/distroless/static-debian13/tags/list)
records this index's `timeUploadedMs` as `1792595040699`
(**2026-10-21 15:04:00.699 UTC**), after the required
**2026-10-20 06:04 UTC** cutoff for the patched CA bundle
([distroless #2215](https://github.com/GoogleContainerTools/distroless/pull/2215)).
Do not use the reproducible config timestamp to infer publication time.

The Lambda arm64 config was created on 2026-10-19 at 23:50:49 UTC, before that
cutoff. AL2023's unpatched `ca-certificates` bundle
([Amazon Linux 2023 #1147](https://github.com/amazonlinux/amazon-linux-2023/issues/1147))
does not block Go **1.27.3 or newer** binaries; it is not a claim that the base
image's bundle is patched. Binfmt's index creation annotation is
2026-06-08 09:32:02 UTC, and BuildKit's is 2026-09-30 15:17:55 UTC; both predate
the cutoff, which applies to the distroless runtime base, not these helpers.

`scripts/check-image-locks.sh` checks repository Dockerfile `FROM` declarations
(including documented examples), helper driver image assignments, and YAML image
inputs for complete SHA-256 pins. The repository's pull-request workflow runs the
check and its isolated positive/negative fixture tests without release secrets.
Custom Dockerfiles in caller repositories are still the caller's responsibility.
Test sources under `scripts/test-*` are excluded; `FROM scratch` and `FROM <stage>` multi-stage forms fail closed unless explicitly annotated with `# image-locks: ignore` on the immediately preceding line for Dockerfiles (trailing comments are not allowed); shell/YAML annotations may be inline.

#### Updating Locks

`renovate.json` adds Dockerfile-template discovery and regex managers for image
locks, helper workflow inputs and setup script, the README's Java example, and tool locks.
Enable the Renovate app for this repository to activate these rules; configuration
alone does not grant the app access. Digest refreshes are grouped into reviewable
PRs, with no automerge. Image tags are retained as readable update channels, not
trusted build identities.
Tagless QEMU and BuildKit execution references are tracked against explicit
`latest` and `buildx-stable-1` update channels, respectively; refreshes replace
their digests without adding mutable tags to the execution references.

Neither Dependabot nor Renovate natively refreshes this repository's arbitrary
artifact SHA-256 lock format. Regex managers instead open **draft version-proposal
PRs** for GoReleaser OSS/Pro, Syft, and Go. Gon is built from a locked source
commit: the `# gon-release <tag>` marker and its following commit lock are tracked
against `ConductorOne/gon` tags. These proposals are not ready-to-merge locks.

Before merging an update:

1. For tools, refresh every affected platform's artifact URL and SHA-256 from the
   reviewed upstream release assets. Keep the version, URL, and checksum together;
   installers reject a version/URL mismatch. For gon, review the source commit
   against its release tag and exercise the source build.
2. For images, resolve the multi-arch index, inspect every architecture with
   `crane manifest`, and record the resolution date and build/publication evidence.
   Keep templates, helper inputs, lock entries, and this table in sync. Distroless
   must remain newer than the CA-bundle cutoff above.
3. Run the image-lock check and relevant installer negative controls, then a
   canary release and artifact/base-layer verification. A bot PR or green fixture
   test is not evidence of a successful release.

### Immutable S3 Release Objects

Versioned release objects are uploaded through S3 `PutObject` with
`If-None-Match: *`. Existing objects under
`releases/{org}/{release_storage_name}/{tag}/...` cause the release to fail
instead of being overwritten. When `release_storage_name` is not set, the
workflow uses the repository name, preserving the existing connector path.

### Public ECR Version Tags

Public ECR image publication writes a temporary candidate tag first. The
workflow compares that candidate digest to any existing version tag before
publishing the version tag. A matching digest is allowed; a different digest is
rejected. The `latest` tag is updated after the version check and is not used
in release identity data.

### Release Source Identity

Release jobs check out caller code by the caller event's `${{ github.sha }}`,
not by a mutable tag reference. `scripts/verify-release-checkout.sh` requires
both HEAD and `refs/tags/<tag>^{commit}` to equal that event SHA before building
or recording metadata. Peeling supports lightweight and annotated tags; a
missing tag, a moved tag, or an event SHA that is a tag object fails closed.
The same event commit remains in the provenance's resolved Git dependency.

Every GoReleaser invocation receives `GORELEASER_CURRENT_TAG` from the workflow input. This pins archive names, release metadata, and image digest files to the requested tag when multiple tags identify the same commit.

### Locked Release Tools

`.github/actions/locked-tools/tool-locks.txt` is the single source of truth for tool versions, artifact
URLs, and committed SHA256 values. The Bash and PowerShell `install-tool`
helpers fetch only that URL, verify the hash **before extraction**, and add the
verified directory to the next step's PATH. They never run the downloaded
binary. All tool installations, including cosign and the native gon build,
finish before Apple certificates, release secrets, or AWS credentials are read.
Both installers require the expected binary layout before publishing PATH; missing tools never silently fall back to runner-bundled binaries.
The workflow checks out its own source using GitHub's `job.workflow_sha`
context, eliminating the previous Python package bootstrap.

Go is installed once per job, with `GOTOOLCHAIN=local`. Separate assertions
check `go version` against the lock and reject any caller/workflow `go.mod`
directive newer than the locked toolchain. Module caching from the former
setup action is intentionally dropped.

The versions below were observed in [release run 38979848074](https://github.com/ConductorOne/baton-github-test/actions/runs/38979848074).
Artifact hashes were fetched once from the upstream release checksums and
[Go's release metadata](https://go.dev/dl/?mode=json), then checked against real
downloads before committing. Checksums and artifacts share upstream TLS origins;
the integrity boundary during a release is the reviewed, committed lock.

Node.js 26.12.1, consumed by axiomatic, is the approved single tool-version change in this series: the uniform patched Node line, superseding the earlier 22.23.4 choice and locked using [SHASUMS256.txt](https://nodejs.org/dist/v26.12.1/SHASUMS256.txt).

The `ConductorOne/github-workflows/.github/actions/locked-tools@<commit-sha>`
composite action accepts a comma-separated `tools` list, an optional reviewed
`locks` file (default: the action's canonical lock), and `dest` (default:
`${{ runner.temp }}/locked-tools`). Invoke it before configuring release
credentials. Supported tools are `go`, `node`, `syft`, `goreleaser`,
`goreleaser-pro`, `gon`, and `minisign`; missing platform locks and unknown or
duplicate tool names fail closed. Requesting `gon` automatically adds Go,
installs/verifies it first, then builds the SHA-checked-out gon source with the
existing readonly-module/proxy/checksum-database constraints.

An empty `locks` input is resolved inside the action's prepare step, where the
action directory is available, rather than in the caller-context input default.
`dest` keeps its `runner.temp` metadata default: that context is available in
the caller on Linux, macOS, and Windows.

Outputs include each installed tool's version (`gon` reports its source commit),
a `versions` JSON object, and `lock_sha256` for provenance. The action verifies
that the lock bytes have not changed between preparation, installation, and
output publication. Each phase is a one-line script call in the composite.
Standalone helpers honor `TOOL_LOCK_FILE` as the same reviewed-lock override.

During the consumer transition, `scripts/install-tool.sh`, `install-tool.ps1`,
`gon-source.sh`, and `verify-locked-go.sh` are thin forwarding wrappers to the
canonical action-local implementations. This keeps this branch's release
workflow unchanged and executable without duplicating implementation or lock
files. The old `tools/tool-locks.txt` is removed, not copied or mirrored.

| Tool / version | Lock platform | SHA256 or source commit | Committed artifact/source |
| --- | --- | --- | --- |
| goreleaser v2.13.3 | linux/amd64 | `4b66f2f78f78561330350651ade557b70328664718490f37834749073af21d20` | [Linux archive](https://github.com/goreleaser/goreleaser/releases/download/v2.13.3/goreleaser_Linux_x86_64.tar.gz) |
| goreleaser v2.13.3 | darwin/arm64 | `5516c37779efb3935d5b213cda3b0b9025ae94ddbcb51df6919acbcdef4194b0` | [Universal Darwin archive](https://github.com/goreleaser/goreleaser/releases/download/v2.13.3/goreleaser_Darwin_all.tar.gz) |
| goreleaser-pro v2.13.3 | windows/amd64 | `8f65fca431ae6aa083805c9287d4b98b7937d54649712f1185b0590ae02001de` | [Windows archive](https://github.com/goreleaser/goreleaser-pro/releases/download/v2.13.3/goreleaser-pro_Windows_x86_64.zip) |
| gon v0.2.10 | darwin/* | `f4c3f2be10a2ee8d50067d2c8a8828a14c83977c` (Git commit, not SHA256) | [Pinned source](https://github.com/ConductorOne/gon/commit/f4c3f2be10a2ee8d50067d2c8a8828a14c83977c) |
| syft v1.42.3 | linux/amd64 | `0d6be741479eddd2c8644a288990c04f3df0d609bbc1599a005532a9dff63509` | [Linux archive](https://github.com/anchore/syft/releases/download/v1.42.3/syft_1.42.3_linux_amd64.tar.gz) |
| syft v1.42.3 | darwin/arm64 | `d71ee7db2be0fe2e96f679fd9d69ef04274cc86c8604707797080a21070b3f32` | [Darwin archive](https://github.com/anchore/syft/releases/download/v1.42.3/syft_1.42.3_darwin_arm64.tar.gz) |
| syft v1.42.3 | windows/amd64 | `e1b9f4945aa64c2b34970bec617623d7f803d0661b48a50b966768b363322e2d` | [Windows archive](https://github.com/anchore/syft/releases/download/v1.42.3/syft_1.42.3_windows_amd64.zip) |
| go go1.27.3 | linux/amd64 | `65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40` | [Linux toolchain](https://go.dev/dl/go1.27.3.linux-amd64.tar.gz) |
| go go1.27.3 | darwin/arm64 | `dd3908cf20959d798c1e06ebe5b72213f0505ce01373b40ce3110975a6cd99c2` | [Darwin toolchain](https://go.dev/dl/go1.27.3.darwin-arm64.tar.gz) |
| go go1.27.3 | windows/amd64 | `f26081f438319757f8a1b46d7d0b2c5ec7583d4038e485e0aa725cfde7885fff` | [Windows toolchain](https://go.dev/dl/go1.27.3.windows-amd64.zip) |
| node v26.12.1 | linux/amd64 | `0fd1eb7770598f52a21995edf6ed35fc690400fa437c2386af329519e9143132` | [Linux package](https://nodejs.org/dist/v26.12.1/node-v26.12.1-linux-x64.tar.gz) |
| node v26.12.1 | darwin/arm64 | `1901d539912c02afa10d1cc126943a8855a7eedebe830119463470db5e6c59e8` | [Darwin package](https://nodejs.org/dist/v26.12.1/node-v26.12.1-darwin-arm64.tar.gz) |
| minisign 0.12 | darwin/arm64 | `89000b19535765f9cffc65a65d64a820f433ef6db8020667f7570e06bf6aac63` | [macOS ZIP](https://github.com/jedisct1/minisign/releases/download/0.12/minisign-0.12-macos.zip), ported from [multipass #953](https://github.com/ductone/multipass/pull/953) |

The gon fork publishes only an amd64 asset for v0.2.10. Rather than using
Rosetta, `actions/checkout` fetches the locked commit by object ID and
`scripts/gon-source.sh` builds it natively with the already-verified Go.
It checks gon's Go requirement, requires `GOFLAGS=-mod=readonly`,
`GOPROXY=https://proxy.golang.org`, and `GOSUMDB=sum.golang.org`, and logs the
built binary's SHA256 for cross-run comparison. A native darwin-arm64 release
asset is a follow-up improvement, not a prerequisite for this source-locked build.

Cosign remains on its observed v2.5.2, installed by
`sigstore/cosign-installer@398d4b0eeef1380460a10c8013a76f728fb906ac`.
That action verifies its bootstrap binary against hashes embedded in the
pinned action and verifies other requested versions by signature. The
failure-only Datadog action is also commit-pinned.

Buildx is never downloaded: `scripts/setup-release-buildx.sh` prints the
runner-bundled plugin's version, fails if it is absent, and creates a builder
with `moby/buildkit@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea`.
QEMU uses `tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0`.
The setup-buildx action was not retained because even `install: false` can
download a plugin when the runner's plugin is unavailable.


### Keyless Signing

All signatures use Sigstore's keyless signing:

- No long-lived keys to manage or rotate
- Identity based on GitHub Actions OIDC token
- Signatures recorded in Rekor transparency log
- Workflow signing and attestation steps run cosign through
  `scripts/cosign-retry.sh`: three attempts with a 10 s then 20 s wait
  (`COSIGN_RETRY_ATTEMPTS`, `COSIGN_RETRY_DELAY`), since the OIDC token
  endpoint and Fulcio fail transiently under load and a repeated signing
  call is harmless

### Provenance Attestations

Every artifact includes SLSA v1 provenance that links it to:

- Source repository and commit
- Workflow that built it (pinned SHA)
- Build environment (runner OS, architecture)
- Build timestamp

### SBOM Attestations

Software Bill of Materials (SPDX format) for each binary:

- Generated by Syft during build
- Signed as in-toto attestation
- Links SBOM to the specific artifact

### Windows MSI Installers

MSI installers are built using WiX Toolset with GoReleaser Pro:

- **UpgradeCode:** Deterministic UUID v5 generated from repository name, ensuring consistent upgrade behavior across versions
- **Default template:** Installs to `C:\Program Files\ConductorOne\<connector>` and adds to PATH
- **Custom templates:** Connectors can provide custom WXS via `msi_wxs_path` input for Windows Service, registry keys, etc.

The MSI uses a WiX-compatible version format (`x.x.x.0`) since WiX doesn't support semver prerelease suffixes.

Both Windows zip and MSI have:
- Cosign signatures (`.sig`, `.cert`)
- SLSA provenance attestations
- SBOM attestations

**Note:** Windows code signing via Azure Trusted Signing is planned for Stage 2.

### Verification

Anyone can verify artifacts using cosign:

```bash
# Verify binary provenance
cosign verify-blob-attestation \
  --bundle artifact.provenance.sigstore.json \
  --type https://slsa.dev/provenance/v1 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp 'https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@.*' \
  artifact.zip

# Verify image provenance
cosign verify-attestation \
  --type https://slsa.dev/provenance/v1 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp 'https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@.*' \
  public.ecr.aws/conductorone/baton-foo@sha256:abc123
```

### Certificate Identity

All attestations are signed with a certificate containing:

- **Issuer:** `https://token.actions.githubusercontent.com`
- **Identity:** `https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@<ref>`

Verification requires matching both issuer and identity pattern.

## Directory Structure

```
$GITHUB_WORKSPACE/
├── _caller/              # Connector repo checkout
│   └── dist/             # GoReleaser output, attestation bundles
└── _workflows/           # github-workflows repo checkout
    ├── _generated/       # Generated configs (goreleaser, gon, predicate)
    ├── _output/          # Final manifest and signatures
    ├── templates/        # Source templates
    └── cmd/              # Go commands
```

## S3 File Structure

```
releases/{org}/{release_storage_name}/{tag}/
├── manifest.json
├── manifest.json.sig
├── manifest.json.cert
├── baton-foo_1.0.0_checksums.txt
├── baton-foo_1.0.0_checksums.txt.sig
├── baton-foo_1.0.0_checksums.txt.cert
├── baton-foo-v1.0.0-darwin-arm64.zip
├── baton-foo-v1.0.0-darwin-arm64.zip.sig
├── baton-foo-v1.0.0-darwin-arm64.zip.cert
├── baton-foo-v1.0.0-darwin-arm64.zip.provenance.sigstore.json
├── baton-foo-v1.0.0-darwin-arm64.zip.sbom.sigstore.json
├── baton-foo-v1.0.0-linux-amd64.tar.gz
├── baton-foo-v1.0.0-linux-amd64.tar.gz.sig
├── baton-foo-v1.0.0-linux-amd64.tar.gz.cert
├── baton-foo-v1.0.0-linux-amd64.tar.gz.provenance.sigstore.json
├── baton-foo-v1.0.0-linux-amd64.tar.gz.sbom.sigstore.json
├── baton-foo-v1.0.0-windows-amd64.zip
├── baton-foo-v1.0.0-windows-amd64.zip.sig
├── baton-foo-v1.0.0-windows-amd64.zip.cert
├── baton-foo-v1.0.0-windows-amd64.zip.provenance.sigstore.json
├── baton-foo-v1.0.0-windows-amd64.zip.sbom.sigstore.json
├── baton-foo_v1.0.0_windows_amd64.msi
├── baton-foo_v1.0.0_windows_amd64.msi.sig
├── baton-foo_v1.0.0_windows_amd64.msi.cert
├── baton-foo_v1.0.0_windows_amd64.msi.provenance.sigstore.json
├── baton-foo_v1.0.0_windows_amd64.msi.sbom.sigstore.json
└── ...
```

## Testing Changes

### Test Repositories

Use test connectors for validation:

- `ConductorOne/baton-runner` - Custom WXS template testing (via `msi_wxs_path` at `ci/app.wxs`)
- `ConductorOne/baton-github-test` - Default WXS template testing

### Testing Process

1. Make workflow changes on a branch
2. Point test repo at your branch:
   ```yaml
   uses: ConductorOne/github-workflows/.github/workflows/release.yaml@your-branch
   ```
3. Create a test release on test connector repo via UI
4. Wait for workflow to complete
5. Validate outputs:
   ```bash
   ./scripts/validate-release-artifacts.sh ConductorOne/baton-github-test v0.1.xxx
   ```

### Local Tool and Source Integrity Checks

`python3 scripts/test-tool-integrity.py` exercises verified archive extraction,
tampered hashes failing before extraction/PATH changes, stale version/URL and
duplicate locks, newer or malformed Go requirements, the source-locked gon
build gates, bundled-buildx failure, and event-SHA checks with both lightweight
and annotated tags. It also checks workflow credential ordering. These are
deterministic local fixtures, not deployed macOS or Windows proof.

Before release adoption, run canaries on `ConductorOne/baton-github-test`:
mutate one artifact hash at a time for GoReleaser and Syft and require the
installer to fail before credentials or binary invocation; mutate the gon
source commit and require checkout/verification to fail before certificates.
Run positive lightweight and annotated tag releases with correct locks and
validate the published artifacts with `scripts/validate-release-artifacts.sh`.
Real platform runs, including PowerShell extraction and native macOS gon
compilation/signing, remain mandatory integration evidence.

### Local Windows Hook Checks

`make test-scripts` includes `python3 scripts/test-windows-pre-build-script.py`.
It uses the real workflow Bash steps and `envsubst`, with `yq` for YAML queries:
empty input must match the frozen pre-hook config fixture byte-for-byte; opted-in
scripts must render the expected command; unsafe and missing paths must fail.
The fixture hook also checks the caller working directory, release-tag
forwarding, and command failure propagation without compiling or publishing.
These are local fixture checks, not a deployed Windows release proof.

`python3 scripts/test-safe-relative-path.py` additionally compares the shared
predicate and every workflow/render call site's exit status and diagnostic bytes
against the previous Bash rules, including extra-file trimming and empty guards.

### Validation Script

The `scripts/validate-release-artifacts.sh` script validates:

- Manifest structure and version match
- All binary assets are downloadable
- Provenance and SBOM attestations exist and verify
- ECR Public image attestations (if present)
- Manifest signature (if present)

```bash
./scripts/validate-release-artifacts.sh ORG/REPO VERSION
```

Exit codes: `0` = all passed, `1` = failures

Each cosign verification is retried on failure, three attempts with a 10 s
then 20 s wait by default (`COSIGN_VERIFY_ATTEMPTS`, `COSIGN_VERIFY_DELAY`),
since Rekor and the certificate transparency log time out and rate-limit
under load. cosign's stderr from the final attempt is printed under a failed
check.

### Manual Verification

```bash
# Download and verify binary provenance
curl -LO "https://dist.conductorone.com/releases/ConductorOne/baton-github-test/v0.1.102/baton-github-test-v0.1.102-darwin-arm64.zip"
curl -LO "https://dist.conductorone.com/releases/ConductorOne/baton-github-test/v0.1.102/baton-github-test-v0.1.102-darwin-arm64.zip.provenance.sigstore.json"

cosign verify-blob-attestation \
  --bundle baton-github-test-v0.1.102-darwin-arm64.zip.provenance.sigstore.json \
  --type https://slsa.dev/provenance/v1 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp 'https://github.com/ConductorOne/github-workflows/.github/workflows/release.yaml@.*' \
  baton-github-test-v0.1.102-darwin-arm64.zip
```

### MSI Installation Testing

Test the MSI installer on an actual Windows machine:

1. Download MSI from S3:
   ```powershell
   curl -LO "https://dist.conductorone.com/releases/ConductorOne/baton-runner/vX.Y.Z/baton-runner_vX.Y.Z_windows_amd64.msi"
   ```

2. Install and verify:
   - Double-click MSI or run `msiexec /i baton-runner_vX.Y.Z_windows_amd64.msi`
   - Check installation path: `C:\Program Files\ConductorOne\baton-runner\`
   - Verify PATH includes installation directory
   - Run `baton-runner --help` from new terminal

3. Test upgrade scenario:
   - Install older version first
   - Install newer version over it
   - Verify clean upgrade (no duplicate entries, old version removed)

4. Test uninstall:
   - Uninstall via Settings > Apps or `msiexec /x`
   - Verify files removed from Program Files
   - Verify PATH entry removed

5. Verify UpgradeCode consistency:
   - UpgradeCode is deterministic UUID v5 from repository name
   - Must be identical across all versions for upgrades to work

### Common Issues

- **Cosign version:** Ensure using latest cosign (`cosign version`)
- **MSI UpgradeCode:** If upgrades don't work, verify UpgradeCode is consistent across versions

## Future Work

### Stage 2: Windows Code Signing

Currently MSI installers have Sigstore signatures (cosign) but not Windows Authenticode signatures. Stage 2 will add:

- **Azure Trusted Signing** integration for Authenticode signatures
- MSI files will be signed with Microsoft-trusted certificate
- Windows SmartScreen warnings will be eliminated
- Users can verify publisher identity in Windows UAC prompts

This requires:
- Azure Trusted Signing account setup
- GitHub Actions OIDC integration with Azure
- Workflow updates to sign MSI after build

### Other Potential Improvements

- Support for Windows ARM64 builds
- Windows Service installer templates (for connectors that run as services)
