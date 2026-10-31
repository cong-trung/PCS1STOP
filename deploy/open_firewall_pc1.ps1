<#
.SYNOPSIS
  Lets the PC2 workers reach the PCS 1-Stop web app on this PC (PC1).  Run it as administrator.

.DESCRIPTION
  Creates (or updates) ONE inbound rule for the app's TCP port.  The rule is known to this script by an internal name,
  not by its display name, so another rule that happens to be called the same is never touched; running the script
  again changes the rule in place, so a wrong value cannot leave you without a rule.

.EXAMPLE
  .\open_firewall_pc1.ps1                                    # the port from ..\config.json, from any PC
  .\open_firewall_pc1.ps1 -RemoteAddress 10.1.2.3,10.1.2.4   # only from these PCs (better)
  .\open_firewall_pc1.ps1 -Remove                            # take the rule away again
#>
[CmdletBinding()]
param(
    [int]$Port = 0,                                  # 0 = server.port of ..\config.json
    [string[]]$RemoteAddress = @('Any'),
    [string]$RuleName = 'PCS 1-Stop (PC1)',          # how the rule is listed in the firewall
    [string[]]$NetworkProfile = @('Domain', 'Private'),
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$ruleId      = 'PCS1Stop-PC1-Inbound'   # the internal name: this is how the script finds its own rule
$description = 'PCS 1-Stop: the web app and the API that the PC2 workers poll.'

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'ERROR: run this from a PowerShell window that was started with "Run as administrator".' -ForegroundColor Red
    exit 1
}

if ($Remove) {
    if (Get-NetFirewallRule -Name $ruleId -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -Name $ruleId
        Write-Host "Removed the firewall rule '$RuleName'."
    } else {
        Write-Host 'This script has not made a firewall rule on this PC - nothing to remove.'
    }
    exit 0
}

if ($Port -le 0) {
    $Port = 8080
    $configPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'config.json'
    if (Test-Path -LiteralPath $configPath) {
        $cfg = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        if ($cfg.server -and $cfg.server.port) { $Port = [int]$cfg.server.port }
    }
}

if (Get-NetFirewallRule -Name $ruleId -ErrorAction SilentlyContinue) {
    # change the rule that is there: a wrong value stops here and the rule that works stays as it was
    Set-NetFirewallRule -Name $ruleId -NewDisplayName $RuleName -LocalPort $Port -RemoteAddress $RemoteAddress `
        -Profile $NetworkProfile -Description $description
} else {
    New-NetFirewallRule -Name $ruleId -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
        -RemoteAddress $RemoteAddress -Profile $NetworkProfile -Description $description | Out-Null
}

Write-Host "Allowed inbound TCP port $Port from $($RemoteAddress -join ', ') on the $($NetworkProfile -join ', ') network profile(s)."
Write-Host 'The app must listen on 0.0.0.0 (server.host in config.json) for this to matter.'
