<#
.SYNOPSIS
  Run the PLATE CI job on Windows, step for step (the Mac `make ci`): fidelity selftest, hazard scan, ruff,
  licences, pytest, scripts tests, claims, npm test, ui build. scripts\tests\test_ci_parity.py keeps this file,
  test.sh, the Makefile, the justfile and ci.yml in step.
  Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\test.ps1
#>
param([switch]$SkipUi)
. "$PSScriptRoot\_common.ps1"
Set-Location $RepoRoot
$py = Get-VenvPython
if (-not (Test-Path $py)) { throw 'No .venv: run scripts\bootstrap.ps1 first' }

Invoke-Step 'fidelity_bench selftest' { & $py -m plate_gates.fidelity_bench selftest }
Invoke-Step 'hazard scanner selftest' { & $py scripts\hazard_scan.py --selftest }
Invoke-Step 'hazard scan' { & $py scripts\hazard_scan.py --strict-allow }
Invoke-Step 'ruff check' { & $py -m ruff check . }
Invoke-Step 'ruff format' { & $py -m ruff format --check . }
Invoke-Step 'licences' { & $py -m plate_pixels.licences --check }
Invoke-Step 'pytest' { & $py -m pytest }
Invoke-Step 'scripts tests' { & $py -m pytest scripts\tests }
Invoke-Step 'claims' { & $py scripts\claims_check.py }
if (-not $SkipUi) {
    Invoke-Step 'npm test' { npm test }
    Invoke-Step 'npm run build' { npm run build }
}
Write-Host 'All PLATE checks passed.' -ForegroundColor Green
