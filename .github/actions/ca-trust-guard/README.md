# CA Trust Guard

## What it checks

CA Trust Guard measures offline incident chains against runner trust stores.
Use it as a job's first step, before checkout or credential setup:

```yaml
steps:
  - uses: ConductorOne/github-workflows/.github/actions/ca-trust-guard@<commit-sha>
```

The five steps are BEFORE probe → optional OS denial → optional clean-bundle
environment → unconditional AFTER probe → summary/enforcement. Fixtures ship
inside the action and are resolved from its location, not a caller checkout.
An OS-denial error is recorded and fails that step. When BEFORE succeeded, an
enabled clean-bundle leg still runs after a failed deny; AFTER and summary run
unless the job was cancelled.
Every probe first validates SHA256SUMS, selects an instrument that passes its
isolation self-check, and verifies **both** positive-control chains against
`vulnerable-roots.pem` before observing physical/client stores. `chains.json`
pins certificate DER fingerprints and capture epochs. File-store measurement
uses explicit CAfile/intermediates and capture time; isolation options are
selected from the binary's advertised capabilities, not its version string.

OpenSSL candidates are tried deterministically in this order:

1. `CA_TRUST_GUARD_OPENSSL`, when set (an environment override, not an action input).
2. `/opt/homebrew/opt/openssl@3/bin/openssl`.
3. `/usr/local/opt/openssl@3/bin/openssl`.
4. `/opt/homebrew/bin/openssl`.
5. `/usr/local/bin/openssl`.
6. `openssl` resolved from PATH.
7. `/usr/bin/openssl`.

The first candidate advertising `-CAfile`, `-untrusted` and `-attime` and passing
the isolation check is selected. `-no-CAfile` is used only when advertised.
`-no-CApath` is likewise conditional; when absent, an empty temporary `-CApath`
neutralizes the default directory. `-no-CAstore` is passed only when advertised (OpenSSL 3.x), avoiding
unsupported-option failures on 1.1.1 and LibreSSL. LibreSSL is accepted only if
the same isolation check succeeds.

The check verifies chain 1 with a temporary CAfile containing only the pinned,
unrelated self-signed `isolation-ca.pem` certificate. It must produce the
missing-issuer rejection, not success or a malformed/empty-store error. A
candidate that still trusts this chain is rejected as leaking implicit trust.
Rejections log path, version and reason and allow the next candidate to run;
only failure of every candidate emits a probe ERROR. The selected instrument
emits an `isolation-self-check` / `VERIFIED-DISTRUST` row before the two positive
controls. This is instrument-control evidence, not physical-store retirement
evidence. Its path and `openssl version` propagate to probe, OS-deny/env records
and summary rows. **Outputs: none.**

Fixture bytes remain SHA-256-pinned with checkout text conversion disabled.
Generated native-tool text is a different boundary: Windows `jq.exe`
[converts LF output to CRLF by default](https://jqlang.org/manual/v1.7/#invoking-jq).
The deny leg normalizes generated TSV/fingerprint output and strips trailing CR
at path consumption; it does not normalize or waive checksums on fixture bytes.

File-store measurement uses OpenSSL because Go 1.27.3 `crypto/x509` applies the
incident SPKI blacklist unconditionally through `containsDistrustedSPKIHash`,
even with explicit roots and historical capture time. Native `x509.Verify`
rejects `vulnerable-roots.pem` with `CertificateInvalidError/NoValidChains`,
preventing the positive control and BEFORE/AFTER store comparisons.

The stdlib-only `cmd/ca-trust-guard` is built using the runner-image Go toolchain
with `GOTOOLCHAIN=local`, `GOPROXY=off` and `GOSUMDB=off`. **Only the bootstrap
build** uses `GO111MODULE=off`: it has no dependencies and a Go 1.22+ language/API
floor, so an older runner Go is not forced to satisfy the repository module's
Go 1.27.3 directive. Vet/tests retain that module/toolchain requirement. Nothing
is downloaded; a missing/too-old compiler fails closed. The runner Go compiler
is a residual bootstrap dependency in the same trust class as the runner agent.

Compiler discovery prefers PATH, then the highest runnable stable cached Go
under `RUNNER_TOOLCACHE` / `RUNNER_TOOL_CACHE` and hosted-toolcache fallbacks
(`arm64` / `x64`), then `/opt/homebrew/bin/go` and `/usr/local/go/bin/go`.
No compiler is downloaded. If none is found, the error lists the probed locations.

The command emits JSON-encoded `ca-trust-guard` rows, and writes raw JSONL
under `$RUNNER_TEMP/ca-trust-guard/`. Its direct interfaces are:

```text
probe --phase before|after --stores <label=path,...> --chains fixtures/chains.json --out <phase>.jsonl
summarize --mode report|enforce --apply-distrust=true|false --os-deny=true|false --in <evidence-dir>
summarize also accepts --os-deny-elevation fail|warn (default fail)
```

`--stores auto` discovers the platform stores. Python certifi, Homebrew,
and Windows Git-Bash OpenSSL discovery subprocesses return **paths only**;
these paths do not decide trust. Go drives the same explicit-store OpenSSL
measurement for every bundle. SecTrust (`security verify-cert`), Node's effective
root dump, and Windows Disallowed lookup observe their respective clients/APIs.
Node's returned PEM is verified as an explicit file store, not by Node TLS.

`fixtures/incident-certs.json` is the single OS-denial identity list: kind
(`root` or `intermediate`), name, SHA-1, SHA-256, pinned PEM source and provenance.
The identities are fingerprint-keyed, including the two intermediates with the
same common name but different cross-signs:

| Certificate | SHA-1 | Source |
| --- | --- | --- |
| emSign ECC Root CA - G3 | `3043FA4FF257DCA0C380EE2E58EA78B23FE6BBC1` | pinned `vulnerable-roots.pem` |
| emSign Root TLS CA - G3 | `39D3236E4A609AB6190CBFA1AD02B3CC61DFC2C3` | pinned `vulnerable-roots.pem` |
| emSign Root CS CA - G3 | `1F74B6010A219FD9CA058D07AA053D15EFD9300D` | pinned `vulnerable-roots.pem` |
| AAA Certificate Services | `D1EB23A46D17D68FD92564C2F1F1601764D8E349` | pinned incident `vulnerable-roots.pem` |
| EM DV TLS CA - G3B (emSign cross-sign) | `A99B289464F3B44343270A7A1E910F02370483FE` | pinned `intermediates-1.pem`, first certificate |
| EM DV TLS CA - G3B (AAA cross-sign) | `1396BE1372BC93DFA2A433CA964DBA91548A6E57` | pinned `intermediates-2.pem`, first certificate |

The deny list contains the four roots and two intermediates in the pinned
fixtures. macOS and Windows deny the intermediate certificates directly;
Linux ca-certificates blacklists the roots and verifies their absence from
the compiled bundle.
Both fingerprints are checked for every candidate before the first mutation.
Unexpected same-name certificates are refused; cross-signed variants are
distinguished by fingerprint. No package upgrade or online fetch is performed.
`bash scripts/deny-os.sh --dry-run` validates candidates and prints mutation
commands without executing them or claiming successful distrust.

## Verdict table

| Verdict | Meaning |
| --- | --- |
| `TRUSTS` | Explicit roots verify the incident chain at capture time, or SecTrust accepts it. |
| `VERIFIED-DISTRUST` | OpenSSL missing-issuer error 20/2, explicit SecTrust deny/not-trusted, or matching Disallowed thumbprint. |
| `REJECTED-UNKNOWN-CAUSE` | SecTrust `CSSMERR_TP_VERIFY_ACTION_FAILED`; rejection is not proven distrust. |
| `UNKNOWN` | Intermediate thumbprint absent from Windows Disallowed; offline revocation blocking is not proven. |
| `ERROR` | Unreadable/empty/malformed store, expired/invalid certificate, failed control, unexpected platform error or failed OS denial. |
| `DELEGATED-OPENSSL` | Node has step-level `--use-openssl-ca`; see the clean-bundle observations. |
| `DENIED` / `BLACKLISTED` | OS mutation succeeded; AFTER rows are the per-store chain observations. |
| `SKIPPED-NOT-ELEVATED` | Windows lacks Administrator, or Unix lacks root/noninteractive sudo; no OS mutation attempted. |

Known SecTrust explicit-deny outcomes include `kSecTrustResultDeny`,
`SecTrustEvaluate result: kSecTrustResultDeny` and other `result: ...Deny...`
variants. `CSSMERR_TP_NOT_TRUSTED` remains verified distrust;
`CSSMERR_TP_VERIFY_ACTION_FAILED` remains unknown-cause rejection; other failures
remain errors.

## Inputs

| Input | Default | Contract |
| --- | --- | --- |
| `mode` | `enforce` | `report` or `enforce`; probe errors always fail closed. |
| `apply-distrust` | `'true'` | Always apply the committed clean-bundle environment when enabled, even on clean baselines. |
| `os-deny` | `'true'` | Deny four roots and two fixture intermediates where supported; host changes persist. |
| `os-deny-elevation` | `fail` | `fail` or `warn` if OS denial cannot run elevated; warn does not waive evidence requirements. |

To observe without mutation, set `mode: report` and disable **both** switches.
The post-condition asserts each mitigation took effect **where it was applied**.

| Condition | Report | Enforce |
| --- | --- | --- |
| Any `ERROR` row | Fail closed | Fail closed |
| `os-deny=false`, clean bundle enabled, BEFORE trusted | Report | Require both clean-bundle chains verified distrusted, plus npm-effective-cafile where present |
| Clean bundle disabled and BEFORE trusted | Report | Freeze, even if OS denial subsequently repairs a store |
| `os-deny=true` | Report actual AFTER rows | Require both chains verified distrusted in the applicable OS store below |
| `os-deny=true` and clean bundle enabled | Report | Also require clean-bundle chains, plus npm-effective-cafile where present |
| Elevation unavailable, policy `fail` | Warning | Fail with `::error` |
| Elevation unavailable, policy `warn` | Warning | Warning; physical and clean-bundle post-conditions still apply |

Applicable OS evidence: Linux `ubuntu-system`; macOS `macos-sectrust`; Windows
both fixture-intermediate thumbprints present in machine Disallowed
(`windows-disallowed-store`). Missing, `TRUSTS`, `UNKNOWN` or unknown-cause
rejection in a required store fails enforce and names that store. macOS
`/etc/ssl/cert.pem` and Homebrew bundles are still observed but **do not gate**:
the deny leg does not mutate them. No file-store mutation is performed.

Self-hosted/non-elevated consumers may knowingly select
`os-deny-elevation: warn`; it does not assert that denial happened. To retain a
clean-bundle-only gate, explicitly set `os-deny: 'false'`; the summary records
`mitigation_scope: clean-bundle-only`. The summary's `residual` retains the
BEFORE baseline; AFTER rows are the actual per-store retirement observations.

## Residual list

- Linux blacklists matched roots in `/etc/ca-certificates.conf`, rebuilds when
  changed, and verifies the compiled bundle. Private certifi and Node bundles
  are not rewritten and may remain residual.
- macOS denies through System.keychain/SecTrust using root or noninteractive
  sudo. `/etc/ssl/cert.pem` and Homebrew bundles are **not** rewritten.
  `macos-brew` is observed only when Homebrew and its ca-certificates bundle are present.
- Elevated Windows imports all six pinned fixture certificates into machine
  Disallowed, independent of prior online chain caching. The four machine/user
  Root/CA stores are still checked for fingerprint/name collisions; their
  original certificates are retained. The offline probe verifies presence of
  the two precise intermediate thumbprints, not a live CryptoAPI TLS handshake.
- Clean-bundle environment covers OpenSSL, curl, Git, Requests, AWS CLI and
  npm/pnpm on Unix. Arbitrary Node script steps also need step-level
  `env: { NODE_OPTIONS: --use-openssl-ca }`; the runner forbids this variable
  through `GITHUB_ENV`. On Windows only Git/curl receive native-path CA settings;
  native CryptoAPI clients are not redirected.
- Action downloads before step 1, the runner agent, runner-image Go bootstrap,
  Windows bundled Node roots, and dockerd/BuildKit TLS remain outside this
  mitigation. Use digest-pinned images and immutable tags for container TLS
  residuals. Never disable TLS verification.
- OS changes persist on self-hosted hosts. Prefer ephemeral runners or plan
  rollback: macOS `sudo security remove-trusted-cert -d <certificate>`; Linux
  remove only the affected `!` prefixes then `update-ca-certificates`; Windows
  remove only the incident thumbprints from `Cert:\LocalMachine\Disallowed`.
  Clean-bundle application also sets global Git CA config and job environment.

## Retirement rule

AFTER **per-store** `VERIFIED-DISTRUST` rows identify repaired physical stores;
they do not assert that every client is repaired. SecTrust denial and the
clean-bundle environment cover different clients. Untouched physical bundles
remain residual until the runner image or that specific store changes;
environment coverage alone does not establish physical-store distrust.
