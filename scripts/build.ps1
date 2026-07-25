$ErrorActionPreference = 'Stop'

# Resolve project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "Building distribution (wheel + sdist)..."
hatch build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
