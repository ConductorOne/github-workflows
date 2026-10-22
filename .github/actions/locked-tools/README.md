# Locked release tools

Install checksum-verified release tools before configuring release credentials:

```yaml
- uses: ConductorOne/github-workflows/.github/actions/locked-tools@<commit-sha>
  id: tools
  with:
    tools: go,node
```

The action owns the version/URL/checksum reader. It resolves its canonical
`tool-locks.txt` from the action directory, not from a caller checkout, verifies
artifacts before extraction, and checks each expected executable leaf before
publishing PATH. Requesting `gon` also installs Go first.

## Inputs and outputs

- `tools`: required comma-separated tool names. Unknown/duplicate tools and
  missing platform locks fail closed.
- `locks`: optional reviewed lock override. Empty selects the action-local
  canonical file; an override is not copied over that canonical file.
- `dest`: installation root, default `${{ runner.temp }}/locked-tools`.

Outputs:

| Output | Contract |
| --- | --- |
| `go`, `node`, `syft`, `goreleaser`, `goreleaser-pro`, `gon`, `minisign` | Installed tool version, or source commit for gon; empty when not requested. Go includes its `go` prefix. |
| `versions` | JSON per-tool map of the installed versions/source commits. |
| `lock_sha256` | SHA256 of the exact reviewed lock used throughout preparation, installation and output publication. |
| `lock_path` | Absolute path of that same existing lock file. Action-local by default; the resolved reviewed override when explicitly supplied. Native Windows form on Windows. |

Prepare exports the resolved path, hash and expected Go version to the later
phases. The action checks the lock hash before each later phase and rejects
changes. When Go is requested, **prepare alone** performs the `sort -u`
single-stable-Go-version sanity across all Go platform rows. The Go gate does
not rediscover versions from a checkout's lock.

## Go gate environment contract

After checking out caller/source helpers, wire the installed action's outputs
into the gate; do not derive its expectations from `_workflows/tool-locks.txt`
or any other checkout copy:

```yaml
- name: Check Go module requirements
  shell: bash
  env:
    EXPECTED_GO_VERSION: ${{ steps.tools.outputs.go }}
    EXPECTED_LOCK_SHA256: ${{ steps.tools.outputs.lock_sha256 }}
    LOCK_PATH: ${{ steps.tools.outputs.lock_path }}
    GOTOOLCHAIN: local
  run: bash _workflows/.github/actions/locked-tools/verify-locked-go.sh module _caller/go.mod _workflows/go.mod
```

The same environment applies to `verify-locked-go.sh version`. Request `go`
(or `gon`, which adds it) to obtain a nonempty expected Go version.

All three expectation variables are required. The gate validates their shape,
requires an absolute readable `LOCK_PATH`, and hashes its bytes to assert the
provided identity. It never parses lock versions. Missing/partial expectations,
a changed lock, nonlocal toolchain mode or a runtime Go-version mismatch fail
closed.

Module mode first rejects missing, duplicate, malformed or too-new `go`
directives. It then checks runtime version equality and runs
`GOTOOLCHAIN=local go list -m` with an explicit module file, readonly/offline
module settings and workspace discovery disabled. The reported GoVersion must
agree with the directive. Neither gate mode can download a Go toolchain.
