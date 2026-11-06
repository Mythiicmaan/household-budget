#Requires -Version 5.1
<#
.SYNOPSIS
    Restart the Household Budget app so it picks up new code.

.DESCRIPTION
    Run from an ADMIN PowerShell window. Your data is not touched.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\restart.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Household Budget',
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run this from an admin PowerShell window.' -ForegroundColor Red
    exit 1
}
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "Scheduled task '$TaskName' not found." -ForegroundColor Red
    exit 1
}

Stop-ScheduledTask -TaskName $TaskName
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*-m budget*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
for ($i = 0; $i -lt 15 -and (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue); $i++) {
    Start-Sleep -Seconds 1
}

Start-ScheduledTask -TaskName $TaskName
$healthy = $false
for ($i = 0; $i -lt 30 -and -not $healthy; $i++) {
    Start-Sleep -Seconds 2
    try { $healthy = ((Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3).status -eq 'ok') } catch { }
}
if ($healthy) {
    Write-Host 'App restarted and answering.' -ForegroundColor Green
} else {
    Write-Host 'The app did not answer. Copy this message to Claude and check C:\BudgetData\logs\app.log' -ForegroundColor Red
    exit 1
}
