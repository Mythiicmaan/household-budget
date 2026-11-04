#Requires -Version 5.1
<#
.SYNOPSIS
    Show whether Household Budget is running as a background service, and how backups are doing.
    Works without admin (some details need admin and are skipped otherwise).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\check_service.ps1
#>
[CmdletBinding()]
param(
    [string]$DataDir = $(if ($env:BUDGET_DATA_DIR) { $env:BUDGET_DATA_DIR } else { 'C:\BudgetData' }),
    [string]$TaskName = 'Household Budget',
    [int]$Port = 8765
)

$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
$signIn = (Get-Process -Name explorer -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1).StartTime
Write-Host "PC started:        $boot"
Write-Host "You signed in:     $signIn"

try {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
    Write-Host "Task last started: $($info.LastRunTime)  (result $($info.LastTaskResult); 267009 = running)"
    if ($signIn -and $info.LastRunTime -lt $signIn) {
        Write-Host '  -> The app started BEFORE you signed in. Startup without sign-in works.' -ForegroundColor Green
    }
} catch {
    Write-Host 'Task details:      need an admin window (or the task is not installed).' -ForegroundColor Yellow
}

# Find the app by the process listening on the port. (Without admin, Windows hides other
# accounts' command lines, so matching on "-m budget" would miss the service.)
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $owner = (Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction SilentlyContinue).User
    if (-not $owner) { $owner = 'another account (run as admin to see which)' }
    Write-Host "App process:       running since $($proc.CreationDate) as $owner"
    if ($signIn -and $proc.CreationDate -lt $signIn) {
        Write-Host '  -> Started BEFORE you signed in: startup without sign-in works.' -ForegroundColor Green
    }
} else {
    Write-Host "App process:       nothing is listening on port $Port" -ForegroundColor Red
}

try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
    Write-Host "Health:            $($h.status)" -ForegroundColor Green
    if ($h.backup.ok) { Write-Host "Backups:           OK, last $($h.backup.last_ok_at)" -ForegroundColor Green }
    else { Write-Host "Backups:           PROBLEM: $($h.backup.problem)" -ForegroundColor Red }
} catch {
    Write-Host "Health:            no answer on http://127.0.0.1:$Port" -ForegroundColor Red
}

$log = Join-Path $DataDir 'logs\app.log'
if (Test-Path $log) {
    Write-Host "`nLast lines of $log"
    Get-Content $log -Tail 8 | ForEach-Object { Write-Host "  $_" }
}
