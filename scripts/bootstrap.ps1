[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location $root
try {
    uv sync --directory backend --group dev --locked
    npm --prefix frontend ci
}
finally {
    Pop-Location
}
