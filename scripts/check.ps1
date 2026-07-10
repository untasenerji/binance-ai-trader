[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location $root
try {
    & "$PSScriptRoot\secret-scan.ps1"
    uv run --directory backend --locked ruff format --check .
    uv run --directory backend --locked ruff check .
    uv run --directory backend --locked mypy
    uv run --directory backend --locked pytest
    npm --prefix frontend run format:check
    npm --prefix frontend run lint
    npm --prefix frontend run typecheck
    npm --prefix frontend run test
    npm --prefix frontend run build
    npm --prefix frontend run test:e2e
}
finally {
    Pop-Location
}
