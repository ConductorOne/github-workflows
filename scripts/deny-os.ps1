param(
    [Parameter(Mandatory=$true)][string]$Manifest,
    [Parameter(Mandatory=$true)][string]$Guard,
    [Parameter(Mandatory=$true)][string]$Out,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
function Row($Verdict, $Detail) {
    if (-not $DryRun) {
        & $Guard record --out $Out --verdict $Verdict --detail $Detail
        if ($LASTEXITCODE -ne 0) { throw 'failed to record OS deny outcome' }
    }
}
$denyList = (Get-Content -Raw -LiteralPath $Manifest | ConvertFrom-Json).certs
if ($denyList.Count -ne 6) { throw 'expected four roots and two compromised intermediates' }
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin -and -not $DryRun) {
    Row 'SKIPPED-NOT-ELEVATED' 'requires Administrator for LocalMachine Disallowed; no mutation attempted'
    exit 0
}
function CheckFingerprint($Cert, $Entry) {
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { $digest = [BitConverter]::ToString($sha256.ComputeHash($Cert.RawData)).Replace('-', '') }
    finally { $sha256.Dispose() }
    if ($Cert.Thumbprint -ne $Entry.sha1 -or $digest -ne $Entry.sha256 -or
        $Cert.Subject -notmatch [regex]::Escape($Entry.name)) {
        throw "fingerprint/name mismatch for $($Entry.name) -- refusing unexpected certificate"
    }
}
# Validate ALL six fixture certificates before the first mutation. Importing
# pinned intermediates does not depend on prior online CryptoAPI chain caching.
$pending = @()
$fixtureDir = Split-Path -Parent $Manifest
foreach ($entry in $denyList) {
    if ([IO.Path]::GetFileName($entry.pem) -ne $entry.pem) { throw 'invalid fixture filename' }
    $pem = Get-Content -Raw -LiteralPath (Join-Path $fixtureDir $entry.pem)
    $blocks = [regex]::Matches($pem, '(?s)-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----')
    $found = $null
    foreach ($block in $blocks) {
        $bytes = [Convert]::FromBase64String($block.Groups[1].Value)
        $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2 -ArgumentList @(,$bytes)
        if ($cert.Thumbprint -eq $entry.sha1) {
            CheckFingerprint $cert $entry
            $found = $cert
            break
        }
    }
    if (-not $found) { throw "fixture fingerprint mismatch for $($entry.name)" }
    $pending += @{ Cert = $found; Entry = $entry }
}
# Retain #951's four-store search and fingerprint/name collision refusal. Both
# compromised intermediates share a CN; accept either AUTHORIZED thumbprint,
# never an unrelated same-name certificate. Original source certs are retained.
$stores = @('Cert:\LocalMachine\Root', 'Cert:\LocalMachine\CA', 'Cert:\CurrentUser\Root', 'Cert:\CurrentUser\CA')
$names = @($denyList | ForEach-Object { $_.name })
$thumbprints = @($denyList | ForEach-Object { $_.sha1 })
foreach ($storePath in $stores) {
    $matches = @(Get-ChildItem -Path $storePath -ErrorAction SilentlyContinue | Where-Object {
        $_.Thumbprint -in $thumbprints -or
        $_.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -in $names
    })
    foreach ($cert in $matches) {
        $entry = @($denyList | Where-Object { $_.sha1 -eq $cert.Thumbprint })
        if ($entry.Count -ne 1) { throw 'unexpected certificate with incident name; refusing mutation' }
        CheckFingerprint $cert $entry[0]
    }
}
$failures = @()
foreach ($item in $pending) {
    $tmp = $null
    try {
        CheckFingerprint $item.Cert $item.Entry
        if ($DryRun) {
            Write-Host "DRY-RUN Import-Certificate $($item.Entry.sha1) into Cert:\LocalMachine\Disallowed"
            continue
        }
        $tmp = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllBytes($tmp, $item.Cert.Export('Cert'))
        Import-Certificate -FilePath $tmp -CertStoreLocation Cert:\LocalMachine\Disallowed | Out-Null
        Row 'DENIED' "$($item.Entry.kind) $($item.Entry.name) ($($item.Entry.sha1)) in Disallowed"
    } catch {
        Write-Error "failed to deny '$($item.Entry.name)' -- $_" -ErrorAction Continue
        $failures += $item.Entry.sha1
    } finally {
        if ($tmp) { Remove-Item $tmp -ErrorAction SilentlyContinue }
    }
}
if ($failures.Count -gt 0) {
    Write-Error "failed to deny trust for $($failures.Count) certificates" -ErrorAction Continue
    exit 1
}
exit 0
