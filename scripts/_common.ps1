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

# Where config.json lives, resolved the way the daemon does (plate_daemon.config.Paths): PLATE_CONFIG wins,
# else <PLATE_HOME or %LOCALAPPDATA%\Plate>\config.json.
function Get-PlateConfigPath {
    if ($env:PLATE_CONFIG) { return $env:PLATE_CONFIG }
    return (Join-Path (Get-PlateHome) 'config.json')
}

# 32 random bytes as URL-safe base64 without padding: the same alphabet as Python's secrets.token_urlsafe(32).
function New-PlateToken {
    $bytes = New-Object 'byte[]' 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Set-ConfigValue($Config, [string]$Name, $Value) {
    if ($Config.PSObject.Properties[$Name]) {
        $Config.$Name = $Value
    } else {
        $Config | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

# Create config.json when it is missing, or add a missing token / daemon_url to an existing one. Keys the
# Max installer owns (repo_path, packets_dir, ...) are kept. Written as UTF-8 without a BOM, LF line endings.
function Initialize-PlateConfig([int]$Port = 4610) {
    $path = Get-PlateConfigPath
    $cfg = $null
    if (Test-Path -LiteralPath $path) {
        $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
        if ($raw -and $raw.Trim()) { $cfg = $raw | ConvertFrom-Json }
    }
    $changed = $false
    if ($null -eq $cfg) { $cfg = New-Object PSObject; $changed = $true }
    if (-not $cfg.PSObject.Properties['token'] -or -not $cfg.token) {
        Set-ConfigValue $cfg 'token' (New-PlateToken)
        $changed = $true
    }
    if (-not $cfg.PSObject.Properties['daemon_url'] -or -not $cfg.daemon_url) {
        Set-ConfigValue $cfg 'daemon_url' "http://127.0.0.1:$Port"
        $changed = $true
    }
    if ($changed) {
        $dir = Split-Path -Parent $path
        if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $json = (($cfg | ConvertTo-Json -Depth 8) -replace "`r`n", "`n") + "`n"
        $tmp = "$path.tmp"
        [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $path -Force
    }
    return $cfg
}

# The bearer token the daemon will use: PLATE_TOKEN_FILE (a file holding only the token) wins over
# config.json["token"]; config.json is created first when it is missing, as the daemon does on start.
function Get-PlateToken([int]$Port = 4610) {
    $cfg = Initialize-PlateConfig -Port $Port
    if ($env:PLATE_TOKEN_FILE) {
        $raw = Get-Content -LiteralPath $env:PLATE_TOKEN_FILE -Raw -Encoding UTF8
        $fileToken = ''
        if ($raw) { $fileToken = $raw.Trim() }
        if ($fileToken.Length -lt 16) { throw "PLATE_TOKEN_FILE=$($env:PLATE_TOKEN_FILE): the token must be at least 16 characters" }
        return $fileToken
    }
    return [string]$cfg.token
}

function Invoke-Step([string]$Name, [scriptblock]$Block) {
    Write-Host "==> $Name" -ForegroundColor Cyan
    # A block with no native command must not trip over the exit code of an earlier step.
    $global:LASTEXITCODE = 0
    & $Block
    if ($global:LASTEXITCODE -ne 0) { throw "$Name failed with exit code $($global:LASTEXITCODE)" }
}
