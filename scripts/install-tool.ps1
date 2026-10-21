# Transitional entry point; implementation and lock live with the action.
param(
    [Parameter(Mandatory = $true)][string]$Tool,
    [Parameter(Mandatory = $true)][string]$DestDir
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot '../.github/actions/locked-tools/install-tool.ps1') -Tool $Tool -DestDir $DestDir
