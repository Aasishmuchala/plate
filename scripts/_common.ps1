# Shared helpers for the PLATE PowerShell scripts. Works in Windows PowerShell 5.1 and pwsh 7.
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PyPathDirs = @('spec', 'daemon', 'pixels', 'engines', 'gates', 'plugins\max', 'tools')
$env:PYTHONPATH = ($PyPathDirs | ForEach-Object { Join-Path $RepoRoot $_ }) -join [IO.Path]::PathSeparator
$env:PYTHONUTF8 = '1'

function Get-PlateHome {
    if ($env:PLATE_HOME) { return $env:PLATE_HOME }
    if ($env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA 'Plate') }
    return (Join-Path $HOME 'Library/Application Support/Plate')
}

function Get-VenvPython {
    $win = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path $win) { return $win }
    return (Join-Path $RepoRoot '.venv/bin/python')
}

function Invoke-Step([string]$Name, [scriptblock]$Block) {
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE" }
}
