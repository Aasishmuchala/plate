<#
.SYNOPSIS
  Run the PLATE test suite on Windows: fidelity selftest, hazard scan, ruff, pytest, npm test (the Mac `make test`).
  Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\test.ps1
#>
param([switch]$SkipUi)
. "$PSScriptRoot\_common.ps1"
Set-Location $RepoRoot
$py = Get-VenvPython
if (-not (Test-Path $py)) { throw 'No .venv: run scripts\bootstrap.ps1 first' }

Invoke-Step 'fidelity_bench selftest' { & $py -m plate_gates.fidelity_bench selftest }
Invoke-Step 'hazard scan' { & $py scripts\hazard_scan.py }
Invoke-Step 'ruff check' { & $py -m ruff check . }
Invoke-Step 'pytest' { & $py -m pytest }
if (-not $SkipUi) { Invoke-Step 'npm test' { npm test } }
Write-Host 'All PLATE tests passed.' -ForegroundColor Green
