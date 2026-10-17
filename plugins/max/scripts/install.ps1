<#
.SYNOPSIS
  Install the PLATE plugin into 3ds Max 2026 for the current user.
.DESCRIPTION
  1. Checks that Max's own Python (3.11) is where Max 2026 puts it. Nothing is installed into it:
     plate_max is standard library only, so no pip package (not even requests) is needed.
  2. Copies plugins\max\startup\plate_startup.py into
     %LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup. An existing copy that differs
     is kept next to it as plate_startup.py.bak.
  3. Creates or updates %LOCALAPPDATA%\Plate\config.json (the file the daemon also uses) with
     token, daemon_url, repo_path and packets_dir. An existing token is never touched unless you pass
     -Token. The first time, the old file is kept as config.json.bak. The keys this script adds are
     remembered in "plate_max_installed_keys" so uninstall.ps1 removes exactly those.
  4. Prints the Safe Scene Script Execution note (see plugins\max\scripts\README.md).
  Works in Windows PowerShell 5.1 and pwsh 7. Never installs or updates anything else, and does
  not touch Max itself. Do not run it in a delivery week (decision 25).
.PARAMETER Token
  The daemon token. Default: the token already in config.json, else 32 new random hex characters.
.PARAMETER DaemonUrl
  Default http://127.0.0.1:4610 (or the value already in config.json).
.PARAMETER RepoPath
  The PLATE clone. Default: the repo this script sits in.
.PARAMETER PacketsDir
  Where packet folders are written (a drive or UNC path). Default: the value already in config.json,
  else a packets folder next to config.json.
.PARAMETER MaxVersion
  Default 2026. Used to find Max's Python and to build the default startup folder.
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
    [string]$PacketsDir = '',
    [string]$MaxVersion = '2026',
    [string]$StartupDir = '',
    [string]$ConfigPath = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$pluginRoot = Split-Path -Parent $PSScriptRoot
if (-not $RepoPath) { $RepoPath = Split-Path -Parent (Split-Path -Parent $pluginRoot) }
if (-not (Test-Path -LiteralPath (Join-Path $RepoPath 'plugins\max\plate_max\__init__.py'))) {
    throw "RepoPath does not look like a PLATE clone: plugins\max\plate_max\__init__.py not found under $RepoPath"
}
# an absolute path: Max's working directory is not this shell's
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
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

# --- Max's Python: look, never write. The expected path is C:\Program Files\Autodesk\3ds Max 2026\Python\python.exe
$programFiles = if ($env:ProgramFiles) { $env:ProgramFiles } else { 'C:\Program Files' }
$maxPython = Join-Path (Join-Path (Join-Path $programFiles 'Autodesk') "3ds Max $MaxVersion") 'Python\python.exe'
if (Test-Path -LiteralPath $maxPython) {
    try {
        $pyVersion = (& $maxPython -c 'import sys; print("%d.%d" % sys.version_info[:2])') | Select-Object -First 1
        if ("$pyVersion" -ne '3.11') {
            Write-Warning "Max's Python reports $pyVersion; plate_max is written for 3.11. Continuing."
        }
        else { Write-Host "Max Python $pyVersion found at $maxPython (nothing is installed into it)." }
    }
    catch { Write-Warning "Could not run $maxPython ($($_.Exception.Message)). Continuing." }
}
else {
    Write-Warning "Max's Python was not found at $maxPython. Continuing: the startup folder is per user, but check the Max version."
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
if (-not $PacketsDir) {
    if ($cfg.Contains('packets_dir') -and $cfg['packets_dir']) { $PacketsDir = [string]$cfg['packets_dir'] }
    else { $PacketsDir = Join-Path (Split-Path -Parent $ConfigPath) 'packets' }
}

foreach ($pair in @(@('token', $Token), @('daemon_url', $DaemonUrl), @('repo_path', $RepoPath), @('packets_dir', $PacketsDir))) {
    $name = $pair[0]
    if (-not $cfg.Contains($name) -and ($added -notcontains $name)) { $added += $name }
    $cfg[$name] = $pair[1]
}
$cfg['plate_max_installed_keys'] = @($added)

$configDir = Split-Path -Parent $ConfigPath
if (-not (Test-Path -LiteralPath $configDir)) { New-Item -ItemType Directory -Force -Path $configDir | Out-Null }
$configBackup = $ConfigPath + '.bak'
if ((Test-Path -LiteralPath $ConfigPath) -and -not (Test-Path -LiteralPath $configBackup)) {
    Copy-Item -LiteralPath $ConfigPath -Destination $configBackup
    Write-Host "  kept the old config as $configBackup"
}
$json = (($cfg | ConvertTo-Json -Depth 8) -replace "`r`n", "`n") + "`n"
# UTF-8 without a BOM: the daemon reads this file with Python's json module
[System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))

# --- startup script
if (-not (Test-Path -LiteralPath $StartupDir)) { New-Item -ItemType Directory -Force -Path $StartupDir | Out-Null }
$target = Join-Path $StartupDir 'plate_startup.py'
if (Test-Path -LiteralPath $target) {
    $same = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if (-not $same) {
        Copy-Item -LiteralPath $target -Destination ($target + '.bak') -Force
        Write-Host "  kept the previous startup script as $target.bak"
    }
}
Copy-Item -LiteralPath $source -Destination $target -Force

Write-Host 'PLATE Max plugin installed.'
Write-Host "  startup script : $target"
Write-Host "  config.json    : $ConfigPath (token $($Token.Substring(0, [Math]::Min(4, $Token.Length)))..., daemon $DaemonUrl)"
Write-Host "  packets        : $PacketsDir"
Write-Host "  repo           : $RepoPath"
Write-Host ''
Write-Host 'Safe Scene Script Execution: the startup script is a user script, not a scene script, but check it once.'
Write-Host '  In Max: Customize > Preferences > Scripting > Safe Scene Script Execution. The startup folder'
Write-Host '  above must not be listed as blocked. Then restart Max: the Listener should print'
Write-Host "  '[PLATE] plate_max ... loaded' and '[PLATE] macro PLATE_Dock registered'."
Write-Host "  Open the dock with the PLATE_Dock macro (Customize User Interface, category PLATE)."
exit 0
