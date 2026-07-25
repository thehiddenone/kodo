$ErrorActionPreference = 'Stop'

# Resolve project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "Formatting source..."
hatch run fmt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
