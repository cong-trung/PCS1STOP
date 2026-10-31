<#
.SYNOPSIS
  Removes the PCS 1-Stop worker's scheduled task (stops the worker; the files in this folder stay).

.DESCRIPTION
  There is one worker per PC, and a newer install replaces an older one.  So that an old copy of this folder cannot
  remove the worker of a newer copy, the task is only removed when it was installed from THIS folder; -Force removes
  it anyway.

.EXAMPLE
  .\uninstall.ps1             # remove the task if it belongs to this folder
  .\uninstall.ps1 -Force      # remove it even if it was installed from another folder
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'PCS1Stop Worker',
    [switch]$Force   # remove the task even when it was installed from another folder
)

$ErrorActionPreference = 'Stop'
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$agent = Join-Path $here 'worker_agent.py'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "There is no scheduled task named '$TaskName' - nothing to remove."
    exit 0
}

$action = @($task.Actions)[0]
$runs   = if ($action) { ([string]$action.Arguments).Trim().Trim('"') } else { '' }
if (-not $Force -and $runs -ne $agent) {   # (-ne ignores upper / lower case)
    $what = if ($runs) { $runs } else { 'something else' }
    Write-Host "ERROR: The scheduled task '$TaskName' runs $what - it was not installed from this folder ($here)." -ForegroundColor Red
    Write-Host '       Run uninstall.bat from the folder it was installed from, or run uninstall.bat -Force to remove it anyway.'
    exit 1
}

try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
} catch {
    Write-Host ("ERROR: Could not remove the scheduled task: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

Write-Host "Removed the scheduled task '$TaskName'. The worker is stopped; delete this folder if you do not need it any more."
