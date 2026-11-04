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

$proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*-m budget*' } | Select-Object -First 1
if ($proc) {
    $owner = (Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction SilentlyContinue).User
    Write-Host "App process:       running since $($proc.CreationDate) as '$(if ($owner) { $owner } else { 'another user' })'"
} else {
    Write-Host 'App process:       NOT running' -ForegroundColor Red
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
