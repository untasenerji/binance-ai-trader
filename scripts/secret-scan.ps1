[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gitleaksCommand = Get-Command gitleaks -ErrorAction SilentlyContinue

if ($null -ne $gitleaksCommand) {
    $gitleaks = $gitleaksCommand.Source
}
else {
    $wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $gitleaks = Get-ChildItem -Path $wingetPackages -Filter "gitleaks.exe" -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime |
        Select-Object -Last 1 -ExpandProperty FullName
}

if ([string]::IsNullOrWhiteSpace($gitleaks)) {
    throw "Gitleaks is required. Install it with: winget install --id Gitleaks.Gitleaks --exact"
}

$config = Join-Path $root ".gitleaks.toml"
$commonArguments = @("--no-banner", "--redact", "--exit-code", "1", "--config", $config)

& $gitleaks git @commonArguments "--log-opts=--all" $root

if ($LASTEXITCODE -ne 0) {
    throw "Secret scan failed for Git history."
}

& $gitleaks dir @commonArguments $root

if ($LASTEXITCODE -ne 0) {
    throw "Secret scan failed for the working tree."
}
