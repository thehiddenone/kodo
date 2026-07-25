$ErrorActionPreference = 'Stop'

# Resolve project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "=== Step 1/4: format ==="
& "$ScriptDir/format.ps1"

Write-Host "=== Step 2/4: build ==="
& "$ScriptDir/build.ps1"

Write-Host "=== Step 3/4: static_analysis ==="
& "$ScriptDir/static_analysis.ps1"

Write-Host "=== Step 4/4: test ==="
& "$ScriptDir/test.ps1"

Write-Host "=== full_build complete ==="
