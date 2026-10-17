<#
.SYNOPSIS
  Remove what install.ps1 added: the startup script and the config.json keys it created.
.DESCRIPTION
  Deletes plate_startup.py from %LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup,
  then removes from config.json only the keys listed in "plate_max_installed_keys" (keys that were
  already there before the install, such as a token the daemon created, stay). If nothing is left,
  config.json is deleted. The PLATE clone and the app-data folder are never touched.
  Works in Windows PowerShell 5.1 and pwsh 7.
.PARAMETER MaxVersion
  Default 2026. Only used to build the default startup folder.
.PARAMETER StartupDir
  Override the Max startup folder.
.PARAMETER ConfigPath
  Override the config file (default: PLATE_CONFIG, then PLATE_HOME\config.json, then
  %LOCALAPPDATA%\Plate\config.json).
#>
[CmdletBinding()]
param(
    [string]$MaxVersion = '2026',
    [string]$StartupDir = '',
    [string]$ConfigPath = ''
)
$ErrorActionPreference = 'Stop'

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

$target = Join-Path $StartupDir 'plate_startup.py'
if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Force
    Write-Host "removed $target"
}
else { Write-Host "no startup script at $target" }

if (Test-Path -LiteralPath $ConfigPath) {
    $cfg = [ordered]@{}
    try {
        $existing = [System.IO.File]::ReadAllText($ConfigPath) | ConvertFrom-Json
        if ($existing) { foreach ($p in $existing.PSObject.Properties) { $cfg[$p.Name] = $p.Value } }
    }
    catch { throw "Could not parse $ConfigPath ($($_.Exception.Message)); leaving it alone." }

    if ($cfg.Contains('plate_max_installed_keys')) {
        foreach ($name in @($cfg['plate_max_installed_keys'])) {
            if ($cfg.Contains($name)) { $cfg.Remove($name) }
        }
        $cfg.Remove('plate_max_installed_keys')
        if ($cfg.Count -eq 0) {
            Remove-Item -LiteralPath $ConfigPath -Force
            Write-Host "removed $ConfigPath (nothing else was in it)"
        }
        else {
            $json = (($cfg | ConvertTo-Json -Depth 8) -replace "`r`n", "`n") + "`n"
            [System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "removed the keys the installer added from $ConfigPath; the others stay"
        }
    }
    else { Write-Host "$ConfigPath has no installer record; left as it is" }
}
else { Write-Host "no config.json at $ConfigPath" }
Write-Host 'PLATE Max plugin uninstalled. Restart 3ds Max.'
exit 0
