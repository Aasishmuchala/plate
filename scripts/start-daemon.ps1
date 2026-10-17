<#
.SYNOPSIS
  Start the PLATE daemon in the foreground on 127.0.0.1:4610 (logs in <app-data>\logs\daemon.*.log).
.DESCRIPTION
  The daemon's stdout goes to logs\daemon.out.log, so this script reads the token from config.json
  (PLATE_TOKEN_FILE, PLATE_CONFIG and PLATE_HOME are honoured exactly as the daemon honours them) and
  prints the launch URL itself before it starts the process. After the start it waits for the daemon's own
  "PLATE daemon http://..." line in the log and echoes it as confirmation.
  -Tailscale additionally binds the machine's Tailscale address; that needs the inbound firewall rule
  printed by scripts\firewall-tailscale.ps1 (and docs\windows.md), scoped to the Tailscale interface only.
  There is no Windows service in v1: register-daemon-task.ps1 starts this script at logon.
#>
param([switch]$Tailscale, [int]$Port = 4610)
. "$PSScriptRoot\_common.ps1"
Set-Location $RepoRoot
$py = Get-VenvPython
if (-not (Test-Path $py)) { throw 'No .venv: run scripts\bootstrap.ps1 first' }
$homeDir = Get-PlateHome
$logDir = Join-Path $homeDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir 'daemon.out.log'
$errLog = Join-Path $logDir 'daemon.err.log'
$hostArg = '127.0.0.1'
if ($Tailscale) {
    $ts = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Tailscale*' -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress
    if (-not $ts) { throw 'No Tailscale IPv4 address found (is Tailscale up?)' }
    $hostArg = "127.0.0.1,$ts"
}
$env:PLATE_BIND = $hostArg
$env:PLATE_PORT = "$Port"
$token = Get-PlateToken -Port $Port
Write-Host "PLATE daemon on $hostArg port $Port (app-data $homeDir)"
Write-Host "PLATE daemon http://127.0.0.1:$Port/#token=$token"
# Start-Process with file redirection: in Windows PowerShell 5.1, piping a native command's stderr while
# ErrorActionPreference is Stop turns the first log line into a terminating error. No -Wait here: the
# confirmation below needs this script to keep running while the daemon starts.
$p = Start-Process -FilePath $py -ArgumentList @('-m', 'plate_daemon') -NoNewWindow -PassThru `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
$null = $p.Handle  # keeps the process handle so ExitCode is readable after WaitForExit (Windows PowerShell 5.1)
$confirmed = $false
$deadline = (Get-Date).AddSeconds(30)
while (-not $confirmed -and -not $p.HasExited -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
    $line = Get-Content -LiteralPath $outLog -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_ -like 'PLATE daemon *' } | Select-Object -First 1
    if ($line) {
        Write-Host "daemon is up: $line" -ForegroundColor Green
        $confirmed = $true
    }
}
if (-not $confirmed) {
    if ($p.HasExited) {
        Write-Warning "the daemon exited with code $($p.ExitCode) before it printed its URL; last lines of ${errLog}:"
    } else {
        Write-Warning "no 'PLATE daemon' line in $outLog after 30 s; still waiting on the process (see $errLog)"
    }
    Get-Content -LiteralPath $errLog -Encoding UTF8 -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
}
$p.WaitForExit()
exit $p.ExitCode
