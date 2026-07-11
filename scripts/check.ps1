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
    & "$PSScriptRoot\secret-scan.ps1"
    Invoke-CheckedCommand -Description "Backend format check" -FilePath "uv" -Arguments @("run", "--directory", "backend", "--locked", "ruff", "format", "--check", ".")
    Invoke-CheckedCommand -Description "Backend lint" -FilePath "uv" -Arguments @("run", "--directory", "backend", "--locked", "ruff", "check", ".")
    Invoke-CheckedCommand -Description "Backend type check" -FilePath "uv" -Arguments @("run", "--directory", "backend", "--locked", "mypy")
    Invoke-CheckedCommand -Description "Backend tests" -FilePath "uv" -Arguments @("run", "--directory", "backend", "--locked", "pytest")
    Invoke-CheckedCommand -Description "Frontend format check" -FilePath "npm" -Arguments @("--prefix", "frontend", "run", "format:check")
    Invoke-CheckedCommand -Description "Frontend lint" -FilePath "npm" -Arguments @("--prefix", "frontend", "run", "lint")
    Invoke-CheckedCommand -Description "Frontend type check" -FilePath "npm" -Arguments @("--prefix", "frontend", "run", "typecheck")
    Invoke-CheckedCommand -Description "Frontend tests" -FilePath "npm" -Arguments @("--prefix", "frontend", "run", "test")
    Invoke-CheckedCommand -Description "Frontend build" -FilePath "npm" -Arguments @("--prefix", "frontend", "run", "build")
    Invoke-CheckedCommand -Description "Frontend E2E tests" -FilePath "npm" -Arguments @("--prefix", "frontend", "run", "test:e2e")
}
finally {
    Pop-Location
}
