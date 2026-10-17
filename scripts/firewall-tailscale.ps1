<#
.SYNOPSIS
  PRINT (never run) the Windows Firewall rule that lets only the Tailscale interface reach the PLATE daemon.
.DESCRIPTION
  The daemon binds 127.0.0.1 only. With scripts\start-daemon.ps1 -Tailscale it also binds the machine's
  Tailscale IPv4 address, and Windows Firewall then needs one inbound rule scoped to that interface. This
  script changes nothing: it prints the command so you can read it, then paste it into an elevated
  PowerShell. Add the rule BEFORE the first -Tailscale start; otherwise the python.exe firewall prompt
  creates a broad rule (allow) or a block rule that overrides the scoped one (deny). Never bind 0.0.0.0.
#>
param([int]$Port = 4610, [string]$InterfaceAlias = 'Tailscale')
$rule = 'New-NetFirewallRule -DisplayName "PLATE daemon (Tailscale only)" -Direction Inbound -Protocol TCP -LocalPort {0} -InterfaceAlias "{1}" -Action Allow -Profile Any' -f $Port, $InterfaceAlias
$undo = 'Remove-NetFirewallRule -DisplayName "PLATE daemon (Tailscale only)"'
Write-Host 'This script only prints. Run the rule from an elevated PowerShell (Run as administrator):'
Write-Host ''
Write-Host $rule
Write-Host ''
Write-Host 'Remove it later with:'
Write-Host $undo
Write-Host ''
Write-Host 'Then start the daemon with: powershell -ExecutionPolicy Bypass -File scripts\start-daemon.ps1 -Tailscale'
