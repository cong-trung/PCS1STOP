<#
.SYNOPSIS
  Installs the PCS 1-Stop worker on this PC.

.DESCRIPTION
  Tests the connection to PC1, then creates a scheduled task that starts the worker whenever you log on (without a
  window, restarted if it stops) and starts it right away.  Run it from the folder it is in, logged on as the user the
  worker should run as: a mapped drive such as M:\ exists only for a logged-on user.

.EXAMPLE
  .\install.ps1               # test the connection, install and start
  .\install.ps1 -DryRun       # only show what would be done
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'PCS1Stop Worker',
    [switch]$SkipCheck,   # do not test the connection to PC1 first
    [switch]$NoStart,     # create the task but do not start it now
    [switch]$DryRun       # change nothing, only say what would happen
)

$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$agent  = Join-Path $here 'worker_agent.py'
$config = Join-Path $here 'worker_config.json'

function Stop-Install([string]$Message) {
    Write-Host ''
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# The full path of a Python 3.10+ python.exe, or $null.
function Get-Python310 {
    $tries = @()
    if (Get-Command py.exe -ErrorAction SilentlyContinue) { $tries += , @('py.exe', '-3') }
    foreach ($name in 'python.exe', 'python3.exe') {
        $found = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { $tries += , @($found.Source) }
    }
    foreach ($try in $tries) {
        $exe   = $try[0]
        $extra = @($try | Select-Object -Skip 1)
        try {
            $out = @(& $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" 2>$null)
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0 -or $out.Count -lt 2) { continue }
        if ([version]($out[0].ToString().Trim()) -ge [version]'3.10') { return $out[1].ToString().Trim() }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $agent))  { Stop-Install "worker_agent.py is not in $here - run this script from the unzipped worker folder." }
if (-not (Test-Path -LiteralPath $config)) { Stop-Install "worker_config.json is not in $here - run this script from the unzipped worker folder." }

$python = Get-Python310
if (-not $python) {
    Stop-Install "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this script again."
}
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-Host "pythonw.exe was not found next to $python - the worker will show a console window." -ForegroundColor Yellow
    $pythonw = $python
}
Write-Host "Python:  $python"
Write-Host "Worker:  $agent"

if ($SkipCheck) {
    Write-Host 'Skipping the connection test.'
} else {
    Write-Host ''
    Write-Host 'Testing the connection to PC1 ...'
    & $python $agent --check
    if ($LASTEXITCODE -ne 0) {
        Stop-Install 'The connection test failed (see above). Fix worker_config.json (server_url, api_token) or PC1, then run this script again.'
    }
}

$who       = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action    = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $agent) -WorkingDirectory $here
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $who
$principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

# A worker that is installed already (from this or another folder) is replaced: there is one worker per PC.
$existing   = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$wasRunning = [bool]$existing -and ($existing.State -eq 'Running')

Write-Host ''
Write-Host "Scheduled task '$TaskName' for ${who}:"
Write-Host '  starts:    at every logon'
Write-Host "  runs:      $pythonw `"$agent`""
Write-Host "  folder:    $here"
Write-Host '  restarts:  every minute after a failure'
if ($existing) {
    $old = @($existing.Actions)[0]
    $oldCommand = if ($old) { ('{0} {1}' -f $old.Execute, $old.Arguments).Trim() } else { 'unknown' }
    Write-Host "  replaces:  the worker that is installed already ($oldCommand)" -ForegroundColor Yellow
}

if ($DryRun) {
    Write-Host ''
    Write-Host 'DRY RUN - nothing was installed.'
    exit 0
}

try {
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue   # (Register -Force replaces its definition)
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'PCS 1-Stop worker: answers the path checks PC1 sends (log: worker.log next to worker_agent.py).' `
        -Force | Out-Null
} catch {
    if ($wasRunning) {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue   # leave the old worker running as it was
    }
    Stop-Install ("Could not create the scheduled task: " + $_.Exception.Message + " - try again from a PowerShell window started with 'Run as administrator'.")
}

Write-Host ''
if ($NoStart) {
    Write-Host "Installed. It starts at the next logon (or run: Start-ScheduledTask -TaskName '$TaskName')."
} else {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host 'Installed and started.'
}
Write-Host ''
Write-Host "  log:        $(Join-Path $here 'worker.log')"
Write-Host '  on PC1:     open /workers - this PC should be listed as ONLINE within a few seconds'
Write-Host '  remove it:  uninstall.bat'
