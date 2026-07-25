$ErrorActionPreference = 'Stop'

# Resolve project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

# Optional: single test or suite selector passed as first argument
# Examples:
#   .\scripts\test.ps1                               # run all tests
#   .\scripts\test.ps1 test\test_orders.py            # run a test file
#   .\scripts\test.ps1 test\test_orders.py::test_refund  # run a single test
#   .\scripts\test.ps1 -k refund                     # pytest -k filter
$Selector = if ($args.Count -gt 0) { $args[0] } else { $null }

if ($Selector) {
    Write-Host "Running test(s) matching: $Selector"
    hatch run test $Selector
} else {
    Write-Host "Running full test suite..."
    hatch run test
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
