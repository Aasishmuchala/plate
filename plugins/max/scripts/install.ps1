<#
.SYNOPSIS
  Install the PLATE plugin into 3ds Max 2026 for the current user.
.DESCRIPTION
  1. Copies plugins\max\startup\plate_startup.py into
     %LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup
  2. Creates or updates %LOCALAPPDATA%\Plate\config.json (the file the daemon also uses) with
     token, daemon_url and repo_path. Keys that were already there are kept, and the keys this
     script adds are remembered in "plate_max_installed_keys" so uninstall.ps1 removes exactly those.
  Works in Windows PowerShell 5.1 and pwsh 7. Never installs or updates anything else, and does
  not touch Max itself. Do not run it in a delivery week (decision 25).
.PARAMETER Token
  The daemon token. Default: the token already in config.json, else 32 new random hex characters.
.PARAMETER DaemonUrl
  Default http://127.0.0.1:4610 (or the value already in config.json).
.PARAMETER RepoPath
  The PLATE clone. Default: the repo this script sits in.
.PARAMETER MaxVersion
  Default 2026. Only used to build the default startup folder.
.PARAMETER StartupDir
  Override the Max startup folder (a non-default Max profile, or a test).
.PARAMETER ConfigPath
  Override the config file (default: PLATE_CONFIG, then PLATE_HOME\config.json, then
  %LOCALAPPDATA%\Plate\config.json).
#>
[CmdletBinding()]
param(
    [string]$Token = '',
    [string]$DaemonUrl = 'http://127.0.0.1:4610',
    [string]$RepoPath = '',
    [string]$MaxVersion = '2026',
    [string]$StartupDir = '',
    [string]$ConfigPath = ''
)
$ErrorActionPreference = 'Stop'

$pluginRoot = Split-Path -Parent $PSScriptRoot
if (-not $RepoPath) { $RepoPath = Split-Path -Parent (Split-Path -Parent $pluginRoot) }
$source = Join-Path (Join-Path $pluginRoot 'startup') 'plate_startup.py'
if (-not (Test-Path -LiteralPath $source)) { throw "startup script not found: $source" }

if (-not $StartupDir) {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is not set; pass -StartupDir' }
    $StartupDir = Join-Path $env:LOCALAPPDATA "Autodesk\3dsMax\$MaxVersion - 64bit\ENU\scripts\startup"
}
if (-not $ConfigPath) {
    if ($env:PLATE_CONFIG) { $ConfigPath = $env:PLATE_CONFIG }
    elseif ($env:PLATE_HOME) { $ConfigPath = Join-Path $env:PLATE_HOME 'config.json' }
    elseif ($env:LOCALAPPDATA) { $ConfigPath = Join-Path (Join-Path $env:LOCALAPPDATA 'Plate') 'config.json' }
    else { throw 'LOCALAPPDATA is not set; pass -ConfigPath' }
}

if ($env:ProgramFiles) {
    $maxDir = Join-Path (Join-Path $env:ProgramFiles 'Autodesk') "3ds Max $MaxVersion"
    if (-not (Test-Path -LiteralPath $maxDir)) {
        Write-Warning "3ds Max $MaxVersion was not found at $maxDir. Continuing: the startup folder is per user."
    }
}

# --- config.json: keep what is there, add what is missing
$cfg = [ordered]@{}
if (Test-Path -LiteralPath $ConfigPath) {
    try {
        $existing = [System.IO.File]::ReadAllText($ConfigPath) | ConvertFrom-Json
        if ($existing) { foreach ($p in $existing.PSObject.Properties) { $cfg[$p.Name] = $p.Value } }
    }
    catch { Write-Warning "Could not parse $ConfigPath ($($_.Exception.Message)); starting a new one." }
}
$added = @()
if ($cfg.Contains('plate_max_installed_keys') -and $cfg['plate_max_installed_keys']) {
    $added = @($cfg['plate_max_installed_keys'])
}

if (-not $Token) {
    if ($cfg.Contains('token') -and $cfg['token']) { $Token = [string]$cfg['token'] }
    else {
        $bytes = New-Object byte[] 16
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($bytes)
        $rng.Dispose()
        $Token = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    }
}
if (-not $PSBoundParameters.ContainsKey('DaemonUrl') -and $cfg.Contains('daemon_url') -and $cfg['daemon_url']) {
    $DaemonUrl = [string]$cfg['daemon_url']
}

foreach ($pair in @(@('token', $Token), @('daemon_url', $DaemonUrl), @('repo_path', $RepoPath))) {
    $name = $pair[0]
    if (-not $cfg.Contains($name) -and ($added -notcontains $name)) { $added += $name }
    $cfg[$name] = $pair[1]
}
$cfg['plate_max_installed_keys'] = @($added)

$configDir = Split-Path -Parent $ConfigPath
if (-not (Test-Path -LiteralPath $configDir)) { New-Item -ItemType Directory -Force -Path $configDir | Out-Null }
$json = (($cfg | ConvertTo-Json -Depth 8) -replace "`r`n", "`n") + "`n"
# UTF-8 without a BOM: the daemon reads this file with Python's json module
[System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))

# --- startup script
if (-not (Test-Path -LiteralPath $StartupDir)) { New-Item -ItemType Directory -Force -Path $StartupDir | Out-Null }
$target = Join-Path $StartupDir 'plate_startup.py'
Copy-Item -LiteralPath $source -Destination $target -Force

Write-Host "PLATE Max plugin installed."
Write-Host "  startup script : $target"
Write-Host "  config.json    : $ConfigPath (token $($Token.Substring(0, [Math]::Min(4, $Token.Length)))..., daemon $DaemonUrl)"
Write-Host "  repo           : $RepoPath"
Write-Host "Restart 3ds Max; the Listener should print '[PLATE] plate_max ... loaded'."
exit 0
