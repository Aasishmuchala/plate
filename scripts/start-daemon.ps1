<#
.SYNOPSIS
  Start the PLATE daemon in the foreground on 127.0.0.1:4610 (logs in <app-data>\logs\daemon.*.log).
.DESCRIPTION
  -Tailscale additionally binds the machine's Tailscale address; that needs the inbound firewall rule
  in docs\windows.md, scoped to the Tailscale interface only. There is no Windows service in v1:
  register-daemon-task.ps1 starts this script at logon.
#>
param([switch]$Tailscale, [int]$Port = 4610)
. "$PSScriptRoot\_common.ps1"
Set-Location $RepoRoot
$py = Get-VenvPython
$homeDir = Get-PlateHome
$logDir = Join-Path $homeDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$hostArg = '127.0.0.1'
if ($Tailscale) {
    $ts = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Tailscale*' -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress
    if (-not $ts) { throw 'No Tailscale IPv4 address found (is Tailscale up?)' }
    $hostArg = "127.0.0.1,$ts"
}
$env:PLATE_BIND = $hostArg
$env:PLATE_PORT = "$Port"
Write-Host "PLATE daemon on $hostArg port $Port (app-data $homeDir)"
# Start-Process with file redirection: in Windows PowerShell 5.1, piping a native command's stderr while
# ErrorActionPreference is Stop turns the first log line into a terminating error.
$p = Start-Process -FilePath $py -ArgumentList @('-m', 'plate_daemon') -NoNewWindow -PassThru -Wait `
    -RedirectStandardOutput (Join-Path $logDir 'daemon.out.log') -RedirectStandardError (Join-Path $logDir 'daemon.err.log')
exit $p.ExitCode
