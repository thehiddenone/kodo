$ErrorActionPreference = 'Stop'

# Resolve project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "Running lint checks..."
hatch run lint
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Running type checks..."
hatch run typecheck
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
