<#
.SYNOPSIS
  Set up PLATE on a Windows 10/11 x64 machine (the render box): uv, Node LTS, the Python 3.12 venv, npm ci,
  and config.json with the daemon token.
.DESCRIPTION
  Installs uv (winget, else the official astral.sh installer) and Node LTS (winget) when they are missing
  (pass -NoInstall to only check), then runs `uv sync --python 3.12` and `npm ci` in the repo (the ui is an
  npm workspace of the root package.json, so one `npm ci` installs it). Never installs the pixels CUDA extra.
  Finally it creates config.json in the app-data folder when it is missing (PLATE_HOME and PLATE_CONFIG are
  honoured) with a fresh token and prints the launch URL.
  Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#>
param([switch]$NoInstall)
. "$PSScriptRoot\_common.ps1"
Set-Location $RepoRoot

function Test-Command([string]$Name) { return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;$HOME\.local\bin"
}

if (-not (Test-Command 'uv')) {
    if ($NoInstall) { throw 'uv is missing. Install it: winget install --id astral-sh.uv -e' }
    Write-Host 'uv not found: installing with winget (astral-sh.uv)'
    if (Test-Command 'winget') {
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    } else {
        Write-Host 'winget not available: using the official uv installer from astral.sh'
        powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    }
    Update-SessionPath
    if (-not (Test-Command 'uv')) { throw 'uv still not on PATH; open a new terminal and re-run bootstrap.ps1' }
}

$nodeOk = $false
if (Test-Command 'node') {
    # package.json engines (from jsdom): ^22.22.2 || ^24.15.0 || >=26.0.0
    $parts = (node --version).TrimStart('v').Split('.')
    $maj = [int]$parts[0]; $min = [int]$parts[1]
    $nodeOk = (($maj -eq 22) -and ($min -ge 22)) -or (($maj -eq 24) -and ($min -ge 15)) -or ($maj -ge 26)
}
if (-not $nodeOk) {
    if ($NoInstall) { throw 'Node 22.22+, 24.15+ or 26+ is missing. Install it: winget install --id OpenJS.NodeJS.LTS -e' }
    if (-not (Test-Command 'winget')) { throw 'Node 22.22+, 24.15+ or 26+ missing and winget unavailable: install Node LTS from nodejs.org' }
    Write-Host 'Node LTS not found: installing with winget (OpenJS.NodeJS.LTS)'
    winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
    Update-SessionPath
    if (-not (Test-Command 'node')) { throw 'node still not on PATH; open a new terminal and re-run bootstrap.ps1' }
}

Invoke-Step 'uv sync (Python 3.12 venv)' { uv sync --python 3.12 }
Invoke-Step 'npm ci (root workspace, includes ui)' { npm ci }

if (-not (Test-Command 'ffmpeg')) {
    Write-Warning 'ffmpeg not on PATH: video-gate tests will be skipped. Install with: winget install --id Gyan.FFmpeg -e (or set PLATE_FFMPEG)'
}
$homeDir = Get-PlateHome
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
Write-Host "PLATE app-data folder: $homeDir"

$port = 4610
if ($env:PLATE_PORT) { $port = [int]$env:PLATE_PORT }
$token = Get-PlateToken -Port $port
Write-Host "PLATE config: $(Get-PlateConfigPath)"
Write-Host 'Bootstrap done. Next: powershell -ExecutionPolicy Bypass -File scripts\test.ps1' -ForegroundColor Green
Write-Host 'Then start the daemon (scripts\start-daemon.ps1) and open this URL; the UI keeps the token and sends it as a Bearer header:'
Write-Host "  http://127.0.0.1:$port/#token=$token"
