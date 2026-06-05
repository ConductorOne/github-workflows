param(
  [Parameter(Mandatory = $true)]
  [string]$Bucket,

  [Parameter(Mandatory = $true)]
  [string]$Directory,

  [Parameter(Mandatory = $true)]
  [string]$BaseDir,

  [switch]$IncludeSbomDocuments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$CacheControl = "public,max-age=31536000,immutable"
$AwsCli = if ($env:AWS_CLI) { $env:AWS_CLI } else { "aws" }

function Get-ReleaseArtifactContentType {
  param([Parameter(Mandatory = $true)][string]$Name)

  switch -Regex ($Name) {
    "\.tar\.gz$" {
      return "application/gzip"
    }
    "\.zip$" {
      return "application/zip"
    }
    "\.msi$" {
      return "application/x-msi"
    }
    "\.sig$" {
      return "application/octet-stream"
    }
    "\.cert$" {
      return "application/x-pem-file"
    }
    "\.sigstore\.json$" {
      return "application/json"
    }
    "\.sbom\.json$" {
      if ($IncludeSbomDocuments) {
        return "application/json"
      }
      return $null
    }
    default {
      return $null
    }
  }
}

function Get-MetadataValue {
  param(
    [Parameter(Mandatory = $true)]$Metadata,
    [Parameter(Mandatory = $true)][string]$Name
  )

  foreach ($Property in $Metadata.PSObject.Properties) {
    if ($Property.Name -ieq $Name) {
      return [string]$Property.Value
    }
  }
  return $null
}

function Invoke-AwsChecked {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)

  $Output = & $AwsCli @Arguments 2>&1
  $ExitCode = $LASTEXITCODE
  if ($ExitCode -ne 0) {
    $Message = ($Output | Out-String).Trim()
    if ($Message) {
      throw "aws $($Arguments -join ' ') failed with exit code ${ExitCode}: ${Message}"
    }
    throw "aws $($Arguments -join ' ') failed with exit code ${ExitCode}"
  }
  return $Output
}

function Get-ExistingObject {
  param(
    [Parameter(Mandatory = $true)][string]$ObjectBucket,
    [Parameter(Mandatory = $true)][string]$ObjectKey
  )

  $Output = & $AwsCli s3api head-object --bucket $ObjectBucket --key $ObjectKey 2>$null
  if ($LASTEXITCODE -ne 0) {
    return $null
  }

  return (($Output | Out-String) | ConvertFrom-Json)
}

if (-not (Test-Path -LiteralPath $BaseDir -PathType Container)) {
  throw "Base directory not found: ${BaseDir}"
}

$UploadCount = 0
$Artifacts = Get-ChildItem -LiteralPath $BaseDir -File
foreach ($Artifact in $Artifacts) {
  $ContentType = Get-ReleaseArtifactContentType -Name $Artifact.Name
  if (-not $ContentType) {
    continue
  }

  $ObjectKey = "$Directory/$($Artifact.Name)"
  $BodySha256 = (Get-FileHash -LiteralPath $Artifact.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
  $Existing = Get-ExistingObject -ObjectBucket $Bucket -ObjectKey $ObjectKey
  if ($null -ne $Existing) {
    $ExistingSha256 = if ($null -ne $Existing.Metadata) {
      Get-MetadataValue -Metadata $Existing.Metadata -Name "sha256"
    } else {
      $null
    }

    if ($ExistingSha256 -eq $BodySha256) {
      Write-Host "S3 object already exists with matching sha256 metadata, skipping: s3://${Bucket}/${ObjectKey}"
      $UploadCount++
      continue
    }

    throw "S3 object already exists with different or missing sha256 metadata: s3://${Bucket}/${ObjectKey}"
  }

  Write-Host "Uploading $($Artifact.Name) to S3 without overwrite..."
  Invoke-AwsChecked -Arguments @(
    "s3api", "put-object",
    "--bucket", $Bucket,
    "--key", $ObjectKey,
    "--body", $Artifact.FullName,
    "--cache-control", $CacheControl,
    "--content-type", $ContentType,
    "--metadata", "sha256=${BodySha256}",
    "--if-none-match", "*"
  ) | Out-Null
  $UploadCount++
}

if ($UploadCount -eq 0) {
  throw "No release artifacts found in ${BaseDir}"
}

Write-Host "Uploaded ${UploadCount} release artifacts to S3"
