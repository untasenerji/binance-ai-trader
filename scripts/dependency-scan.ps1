[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments

    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

Push-Location $root
try {
    Invoke-CheckedCommand -Description "Python dependency audit" -FilePath "uv" -Arguments @("run", "--directory", "backend", "--locked", "pip-audit")
    Invoke-CheckedCommand -Description "Frontend dependency audit" -FilePath "npm" -Arguments @("--prefix", "frontend", "audit", "--audit-level=high")
}
finally {
    Pop-Location
}
