Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $PSCommandPath
$RootDir = Split-Path -Parent $ScriptDir
$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $TempDir | Out-Null

try {
  $FakeAws = Join-Path $TempDir "aws.ps1"
  $ArgsLog = Join-Path $TempDir "aws-args.log"
  Set-Content -LiteralPath $FakeAws -Encoding utf8 -Value @'
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArgs)

foreach ($Arg in $AwsArgs) {
  Add-Content -LiteralPath $env:AWS_ARGS_LOG -Value $Arg
}

if ($AwsArgs.Count -ge 2 -and $AwsArgs[0] -eq "s3api" -and $AwsArgs[1] -eq "head-object") {
  if ($env:FAKE_HEAD_OBJECT_JSON) {
    Write-Output $env:FAKE_HEAD_OBJECT_JSON
    exit 0
  }
  exit 255
}

if ($AwsArgs.Count -ge 2 -and $AwsArgs[0] -eq "s3api" -and $AwsArgs[1] -eq "put-object" -and $env:FAKE_AWS_FAIL_PUT) {
  Write-Error "put-object failed"
  exit 254
}

exit 0
'@

  $DistDir = Join-Path $TempDir "dist"
  New-Item -ItemType Directory -Path $DistDir | Out-Null
  Set-Content -LiteralPath (Join-Path $DistDir "example.zip") -Value "zip"
  Set-Content -LiteralPath (Join-Path $DistDir "example.zip.sig") -Value "sig"
  Set-Content -LiteralPath (Join-Path $DistDir "example.zip.cert") -Value "cert"
  Set-Content -LiteralPath (Join-Path $DistDir "example.zip.sbom.json") -Value "{}"
  Set-Content -LiteralPath (Join-Path $DistDir "example.zip.sbom.sigstore.json") -Value "{}"
  Set-Content -LiteralPath (Join-Path $DistDir "ignore.txt") -Value "ignore"

  $env:AWS_CLI = $FakeAws
  $env:AWS_ARGS_LOG = $ArgsLog
  Remove-Item Env:\FAKE_HEAD_OBJECT_JSON -ErrorAction SilentlyContinue
  Remove-Item Env:\FAKE_AWS_FAIL_PUT -ErrorAction SilentlyContinue

  & (Join-Path $RootDir "scripts/upload-release-artifacts.ps1") `
    -Bucket "release-bucket" `
    -Directory "releases/ConductorOne/example/v1.2.3" `
    -BaseDir $DistDir `
    -IncludeSbomDocuments

  $Log = Get-Content -LiteralPath $ArgsLog
  foreach ($Expected in @(
      "releases/ConductorOne/example/v1.2.3/example.zip",
      "releases/ConductorOne/example/v1.2.3/example.zip.sig",
      "releases/ConductorOne/example/v1.2.3/example.zip.cert",
      "releases/ConductorOne/example/v1.2.3/example.zip.sbom.json",
      "releases/ConductorOne/example/v1.2.3/example.zip.sbom.sigstore.json",
      "--if-none-match",
      "*",
      "--metadata")) {
    if ($Log -notcontains $Expected) {
      throw "missing expected AWS argument: ${Expected}"
    }
  }
  if ($Log -contains "ignore.txt") {
    throw "unexpected upload for ignore.txt"
  }

  $FailDir = Join-Path $TempDir "fail-dist"
  New-Item -ItemType Directory -Path $FailDir | Out-Null
  Set-Content -LiteralPath (Join-Path $FailDir "failure.zip") -Value "zip"
  Clear-Content -LiteralPath $ArgsLog
  $env:FAKE_AWS_FAIL_PUT = "1"

  $Failed = $false
  try {
    & (Join-Path $RootDir "scripts/upload-release-artifacts.ps1") `
      -Bucket "release-bucket" `
      -Directory "releases/ConductorOne/example/v1.2.3" `
      -BaseDir $FailDir
  } catch {
    $Failed = $true
  }

  if (-not $Failed) {
    throw "put-object failure should fail the upload script"
  }

  Write-Host "PowerShell S3 release upload tests passed"
} finally {
  Remove-Item -Recurse -Force -LiteralPath $TempDir -ErrorAction SilentlyContinue
  Remove-Item Env:\AWS_CLI -ErrorAction SilentlyContinue
  Remove-Item Env:\AWS_ARGS_LOG -ErrorAction SilentlyContinue
  Remove-Item Env:\FAKE_HEAD_OBJECT_JSON -ErrorAction SilentlyContinue
  Remove-Item Env:\FAKE_AWS_FAIL_PUT -ErrorAction SilentlyContinue
}
