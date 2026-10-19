<#
.SYNOPSIS
  Recheck PLATE on this Windows box in one go: bootstrap, the whole test suite, the opt-in stress target,
  the end-to-end smoke, and (with -Max) the 3ds Max plugin install. Every step is recorded with its exit
  code and duration in %LOCALAPPDATA%\Plate\recheck-<stamp>.txt; the script keeps going after a failure so
  one report shows everything, and exits 1 if any step failed.
  Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\recheck.ps1 [-Max] [-SkipUi] [-NoStress]
#>
param([switch]$Max, [switch]$SkipUi, [switch]$NoStress)
. "$PSScriptRoot\_common.ps1"
Set-Location $RepoRoot
$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$reportDir = Join-Path $env:LOCALAPPDATA 'Plate'
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$report = Join-Path $reportDir ("recheck-" + $stamp + ".txt")
$failed = 0
$lines = @()
$lines += "PLATE recheck " + $stamp + " on " + $env:COMPUTERNAME + " at " + $RepoRoot
$lines += "commit " + (git rev-parse --short HEAD)

function Run-Step([string]$Name, [scriptblock]$Body) {
    Write-Host ("=== " + $Name) -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $global:LASTEXITCODE = 0
    $ok = $true
    try {
        & $Body
        if ($LASTEXITCODE -ne 0) { $ok = $false }
    } catch {
        Write-Host $_ -ForegroundColor Red
        $ok = $false
    }
    $sw.Stop()
    $verdict = 'ok'
    if (-not $ok) { $verdict = 'FAILED'; $script:failed += 1 }
    $line = ("{0,-28} {1,-7} {2,8:N0} s" -f $Name, $verdict, $sw.Elapsed.TotalSeconds)
    $script:lines += $line
    Write-Host $line
}

Run-Step 'bootstrap' { & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'bootstrap.ps1') }
$py = Get-VenvPython
if (-not (Test-Path $py)) {
    $lines += 'no .venv after bootstrap: stopping'
    $failed += 1
} else {
    $testArgs = @('-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'test.ps1'))
    if ($SkipUi) { $testArgs += '-SkipUi' }
    Run-Step 'test suite (test.ps1)' { & powershell @testArgs }
    if (-not $NoStress) {
        Run-Step 'stress target' { & $py scripts\stress\run_all.py }
    }
    $smokeArgs = @('scripts\smoke_e2e.py')
    if (-not $SkipUi) { $smokeArgs += '--build-ui' }
    Run-Step 'end-to-end smoke' { & $py @smokeArgs }
    if ($Max) {
        Run-Step 'Max plugin install' {
            & powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot 'plugins\max\scripts\install.ps1') -RepoPath $RepoRoot
        }
    }
}
$lines += ''
if ($failed -eq 0) { $lines += 'RECHECK PASS' } else { $lines += ("RECHECK FAIL: " + $failed + " step(s)") }
$lines | Set-Content -Path $report -Encoding ASCII
Write-Host ''
$lines | ForEach-Object { Write-Host $_ }
Write-Host ("report: " + $report)
if ($failed -ne 0) { exit 1 }
exit 0
