<#
.SYNOPSIS
  Authenticode-sign a Windows artifact (.exe or .msi) with Azure Trusted Signing.

.DESCRIPTION
  Thin, idempotent wrapper around the Microsoft `sign` dotnet CLI tool
  (https://github.com/dotnet/sign) configured for Azure Trusted Signing.

  Authentication is handled by Azure.Identity's credential chain, which the
  preceding `azure/login` step populates via GitHub Actions OIDC (no long-lived
  certificate material is stored anywhere in this repo or its secrets).

  This script is invoked from the GoReleaser Windows pipeline in two places:
    1. As a build post-hook, to sign the raw `.exe` BEFORE it is packaged into
       the zip archive and the MSI installer (so every distributed copy of the
       binary carries the signature).
    2. As a GoReleaser `signs` entry, to sign the `.msi` installer in place,
       ordered BEFORE the cosign signature so the Sigstore bundle covers the
       final, Authenticode-signed bytes.

.PARAMETER Path
  Path to the artifact to sign. Signing is performed in place.

.PARAMETER EmitMarker
  When set, writes a sidecar `<Path>.authenticode.json` describing the applied
  signature. GoReleaser's `signs` stage expects each signer to produce a
  distinct signature output file; the MSI signer points its `signature` at this
  marker so GoReleaser does not collide the signature path with the in-place
  signed installer. The marker is informational and is not uploaded to S3
  (the release uploader ignores unknown extensions).

.NOTES
  Required environment variables (set by the release workflow when
  windows_authenticode_signing is enabled):
    TRUSTED_SIGNING_ENDPOINT             - e.g. https://wus2.codesigning.azure.net/
    TRUSTED_SIGNING_ACCOUNT_NAME         - Trusted Signing account name
    TRUSTED_SIGNING_CERTIFICATE_PROFILE  - certificate profile name

  Optional:
    TRUSTED_SIGNING_TIMESTAMP_URL  - RFC 3161 timestamp authority
                                     (default: http://timestamp.acs.microsoft.com)
#>
param(
  [Parameter(Mandatory = $true)]
  [string]$Path,

  [switch]$EmitMarker
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
  throw "Artifact to sign not found: $Path"
}

$Endpoint = $env:TRUSTED_SIGNING_ENDPOINT
$Account = $env:TRUSTED_SIGNING_ACCOUNT_NAME
# Note: avoid the name $Profile, which is a PowerShell automatic variable.
$CertProfile = $env:TRUSTED_SIGNING_CERTIFICATE_PROFILE
$TimestampUrl = if ($env:TRUSTED_SIGNING_TIMESTAMP_URL) {
  $env:TRUSTED_SIGNING_TIMESTAMP_URL
} else {
  "http://timestamp.acs.microsoft.com"
}

foreach ($pair in @(
    @{ Name = "TRUSTED_SIGNING_ENDPOINT"; Value = $Endpoint },
    @{ Name = "TRUSTED_SIGNING_ACCOUNT_NAME"; Value = $Account },
    @{ Name = "TRUSTED_SIGNING_CERTIFICATE_PROFILE"; Value = $CertProfile }
  )) {
  if ([string]::IsNullOrWhiteSpace($pair.Value)) {
    throw "Required environment variable $($pair.Name) is not set"
  }
}

$FullPath = (Resolve-Path -LiteralPath $Path).Path
Write-Host "Authenticode-signing via Azure Trusted Signing: $FullPath"

# `sign code trusted-signing` signs the file in place. The Azure Trusted Signing
# service applies the timestamp, so SmartScreen reputation is tied to the
# Microsoft-trusted certificate rather than to download volume.
& sign code trusted-signing `
  --trusted-signing-endpoint $Endpoint `
  --trusted-signing-account $Account `
  --trusted-signing-certificate-profile $CertProfile `
  --timestamp-url $TimestampUrl `
  --file-digest SHA256 `
  --timestamp-digest SHA256 `
  --verbosity information `
  $FullPath

if ($LASTEXITCODE -ne 0) {
  throw "sign code trusted-signing failed for ${FullPath} with exit code ${LASTEXITCODE}"
}

# Fail loudly if the file did not end up validly signed.
$sig = Get-AuthenticodeSignature -LiteralPath $FullPath
if ($sig.Status -ne "Valid") {
  throw "Authenticode signature is not valid for ${FullPath}: status=$($sig.Status) message=$($sig.StatusMessage)"
}

Write-Host "Signed and verified: $FullPath (signer: $($sig.SignerCertificate.Subject))"

if ($EmitMarker) {
  $markerPath = "$FullPath.authenticode.json"
  $marker = [ordered]@{
    artifact     = (Split-Path -Leaf $FullPath)
    status       = [string]$sig.Status
    signer       = [string]$sig.SignerCertificate.Subject
    thumbprint   = [string]$sig.SignerCertificate.Thumbprint
    timestamp    = [string]$sig.TimeStamperCertificate.Subject
    signedAtUtc  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  }
  $marker | ConvertTo-Json -Depth 4 | Out-File -LiteralPath $markerPath -Encoding utf8
  Write-Host "Wrote signature marker: $markerPath"
}
