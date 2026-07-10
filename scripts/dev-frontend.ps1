[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location $root
try {
    npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173
}
finally {
    Pop-Location
}
