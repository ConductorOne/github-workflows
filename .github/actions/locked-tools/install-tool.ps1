# Install only a committed artifact; never execute downloaded code here.
param(
    [Parameter(Mandatory = $true)][string]$Tool,
    [Parameter(Mandatory = $true)][string]$DestDir
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $IsWindows -or [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw '::error title=locked-tools::Unsupported platform (expected windows/amd64)'
}
$lock = if ($env:TOOL_LOCK_FILE) { $env:TOOL_LOCK_FILE } else { Join-Path $PSScriptRoot 'tool-locks.txt' }
$rows = @(Get-Content $lock | Where-Object { $_ -notmatch '^\s*(#|$)' } | ForEach-Object {
    $fields = $_ -split '\s+'
    if ($fields[0] -eq $Tool -and $fields[2] -eq 'windows/amd64') { ,$fields }
})
if ($rows.Count -ne 1 -or $rows[0].Count -ne 5) {
    throw "::error title=locked-tools::Expected exactly one artifact lock for $Tool windows/amd64"
}
$version = $rows[0][1]
$expected = $rows[0][3]
$url = $rows[0][4]
if ($expected -cnotmatch '^[0-9a-f]{64}$') {
    throw "::error title=locked-tools::Invalid sha256 lock for $Tool"
}
switch ($Tool) {
    'go' { $canonical = "https://go.dev/dl/$version.windows-amd64.zip" }
    'syft' { $canonical = "https://github.com/anchore/syft/releases/download/$version/syft_$($version.TrimStart('v'))_windows_amd64.zip" }
    'goreleaser-pro' { $canonical = "https://github.com/goreleaser/goreleaser-pro/releases/download/$version/goreleaser-pro_Windows_x86_64.zip" }
    'node' {
        $package = "node-$version-win-x64"
        $canonical = "https://nodejs.org/dist/$version/$package.zip"
    }
    default { throw "::error title=locked-tools::Unsupported artifact tool: $Tool" }
}
if ($url -cne $canonical) {
    throw "::error title=locked-tools::$Tool lock version/platform does not match its URL"
}
$workdir = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $workdir | Out-Null
try {
    $archive = Join-Path $workdir 'artifact.zip'
    Invoke-WebRequest -Uri $url -OutFile $archive -MaximumRetryCount 3
    $actual = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash.ToLowerInvariant()
    if ($actual -cne $expected) {
        Write-Host "::error title=locked-tools::$Tool sha256 mismatch"
        throw "Expected $expected, got $actual"
    }
    # Do not extract anything until the committed hash has matched.
    $DestDir = [System.IO.Path]::GetFullPath($DestDir)
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    Expand-Archive -Path $archive -DestinationPath $DestDir -Force
    if ($Tool -eq 'go') {
        $bin = Join-Path $DestDir 'go/bin'
    } elseif ($Tool -eq 'node') {
        $bin = Join-Path $DestDir $package
    } else {
        $bin = $DestDir
    }
    $binaryName = if ($Tool -eq 'goreleaser-pro') { 'goreleaser.exe' } else { "$Tool.exe" }
    $binaryPath = Join-Path $bin $binaryName
    if (-not (Test-Path -LiteralPath $binaryPath -PathType Leaf)) {
        Write-Host "::error title=locked-tools::$Tool package layout mismatch"
        throw "Expected tool binary not found at $binaryPath"
    }
    if ($Tool -eq 'go') {
        if (-not $env:GITHUB_ENV) { throw 'GITHUB_ENV must be set' }
        "GOROOT=$(Join-Path $DestDir 'go')" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
    }
    if (-not $env:GITHUB_PATH) { throw 'GITHUB_PATH must be set' }
    # GitHub prepends these entries to PATH for subsequent steps.
    $bin | Out-File -FilePath $env:GITHUB_PATH -Encoding utf8 -Append
    Write-Host "Installed $Tool $version (windows/amd64), sha256 verified"
} finally {
    Remove-Item -Recurse -Force $workdir
}
