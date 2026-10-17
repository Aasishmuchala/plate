<#
.SYNOPSIS
  Register (or with -Unregister remove) a per-user Task Scheduler task that runs start-daemon.ps1 at logon.
  No admin rights needed; no Windows service in v1.
#>
param([switch]$Unregister, [switch]$Tailscale)
$ErrorActionPreference = 'Stop'
$TaskName = 'PLATE daemon'
if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'"
    return
}
$script = Join-Path $PSScriptRoot 'start-daemon.ps1'
$taskArgs = "-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
if ($Tailscale) { $taskArgs += ' -Tailscale' }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description 'PLATE finishing daemon (127.0.0.1:4610)' -Force | Out-Null
Write-Host "Registered '$TaskName' to start at logon for $env:USERNAME"
