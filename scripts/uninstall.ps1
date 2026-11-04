#Requires -Version 5.1
<#
.SYNOPSIS
    Undo install.ps1: remove the scheduled task and the service account's permissions.

.DESCRIPTION
    Run from an ADMIN PowerShell window. Your data and backups are NOT touched.
    Add -RemoveUser to also delete the 'budgetsvc' account. Power settings are left as-is.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -RemoveUser
#>
[CmdletBinding()]
param(
    [string]$DataDir = $(if ($env:BUDGET_DATA_DIR) { $env:BUDGET_DATA_DIR } else { 'C:\BudgetData' }),
    [string]$BackupDir = 'E:\Backups\HouseholdBudget',
    [string]$ServiceUser = 'budgetsvc',
    [string]$TaskName = 'Household Budget',
    [switch]$RemoveUser
)
$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $PSScriptRoot

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run this from an admin PowerShell window.' -ForegroundColor Red
    exit 1
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
}

$PyHome = (Get-Content (Join-Path $RepoDir '.venv\pyvenv.cfg') -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1) -replace '^\s*home\s*=\s*', ''
foreach ($p in @($DataDir, $BackupDir, $RepoDir, $PyHome)) {
    if ($p -and (Test-Path $p)) {
        icacls $p /remove:g $ServiceUser /T /C /Q | Out-Null
        Write-Host "Removed '$ServiceUser' permissions from $p"
    }
}

if ($RemoveUser -and (Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue)) {
    $sid = (Get-LocalUser -Name $ServiceUser).SID.Value
    $inf = Join-Path $env:TEMP ("hb-rights-{0}.inf" -f [guid]::NewGuid())
    $sdb = Join-Path $env:TEMP ("hb-rights-{0}.sdb" -f [guid]::NewGuid())
    try {
        secedit /export /cfg $inf /areas USER_RIGHTS | Out-Null
        $lines = @(Get-Content $inf) | ForEach-Object {
            if ($_ -match '^\s*SeBatchLogonRight\s*=') {
                ($_ -replace (',?\*' + [regex]::Escape($sid) + '(?![\d-])'), '') -replace '=\s*,', '= '
            } else { $_ }
        }
        Set-Content -Path $inf -Value $lines -Encoding Unicode
        secedit /configure /db $sdb /cfg $inf /areas USER_RIGHTS | Out-Null
    } finally {
        Remove-Item $inf, $sdb, ($sdb -replace '\.sdb$', '.jfm') -ErrorAction SilentlyContinue
    }
    Remove-LocalUser -Name $ServiceUser
    Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList' `
        -Name $ServiceUser -ErrorAction SilentlyContinue
    Write-Host "Removed account '$ServiceUser'. (Its empty profile folder under C:\Users can be deleted by hand.)"
}
Write-Host 'Done. Data and backups were not touched.' -ForegroundColor Green
