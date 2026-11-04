#Requires -Version 5.1
<#
.SYNOPSIS
    Restore the database from a backup file.

.DESCRIPTION
    Run from an ADMIN PowerShell window (it stops and restarts the scheduled task).
    Asks for confirmation. The current database is renamed aside, never deleted.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\restore.ps1 E:\Backups\HouseholdBudget\budget-2027-01-31.db
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupFile,
    [string]$DataDir = $(if ($env:BUDGET_DATA_DIR) { $env:BUDGET_DATA_DIR } else { 'C:\BudgetData' }),
    [string]$TaskName = 'Household Budget',
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoDir '.venv\Scripts\python.exe'

if (-not (Test-Path $BackupFile)) { Write-Host "Backup file not found: $BackupFile" -ForegroundColor Red; exit 1 }
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Run this from an admin PowerShell window (it needs to stop the app).' -ForegroundColor Red
        exit 1
    }
}

Write-Host "This replaces $DataDir\budget.db with:" -ForegroundColor Yellow
Write-Host "    $BackupFile ($((Get-Item $BackupFile).LastWriteTime))" -ForegroundColor Yellow
Write-Host 'Anything entered after that backup will not be in the restored database' -ForegroundColor Yellow
Write-Host '(the current database is kept as budget-before-restore-<time>.db).' -ForegroundColor Yellow
if ((Read-Host 'Type RESTORE to continue') -cne 'RESTORE') { Write-Host 'Cancelled. Nothing changed.'; exit 1 }

if ($task) {
    Stop-ScheduledTask -TaskName $TaskName
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*-m budget*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
for ($i = 0; $i -lt 15 -and (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue); $i++) {
    Start-Sleep -Seconds 1
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "The app is still running on port $Port (close any terminal running it). Nothing changed." -ForegroundColor Red
    if ($task) { Start-ScheduledTask -TaskName $TaskName }
    exit 1
}

$env:BUDGET_DATA_DIR = $DataDir
& $Python -m budget restore $BackupFile --yes
$ok = ($LASTEXITCODE -eq 0)

if ($task) {
    Start-ScheduledTask -TaskName $TaskName
    $healthy = $false
    for ($i = 0; $i -lt 30 -and -not $healthy; $i++) {
        Start-Sleep -Seconds 2
        try { $healthy = ((Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3).status -eq 'ok') } catch { }
    }
    if ($healthy) { Write-Host 'App restarted and answering.' -ForegroundColor Green }
    else { Write-Host "App did not answer; check $DataDir\logs\app.log" -ForegroundColor Red; exit 1 }
}
if (-not $ok) { exit 1 }
Write-Host 'Restore complete.' -ForegroundColor Green
