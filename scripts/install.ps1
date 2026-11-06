#Requires -Version 5.1
<#
.SYNOPSIS
    One-time setup so Household Budget runs 24/7 on this PC, before anyone signs in.

.DESCRIPTION
    Run from an ADMIN PowerShell window. Safe to run again (it updates what exists).
      1. Creates a standard (non-admin) local account that runs the app.
      2. Lets that account run scheduled tasks ("Log on as a batch job").
      3. Gives it Modify on the data and backup folders, Read on the code and Python.
      4. Sets power: never sleep on AC power, screen off after 10 minutes.
      5. Registers the "Household Budget" scheduled task: at startup, whether or not
         anyone is signed in, restart on failure (3 tries, 1 minute apart).
      6. Makes a first backup, starts the app, and checks that it answers.
    The app listens on 127.0.0.1 only. No firewall rules or port forwarding are added.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>
[CmdletBinding()]
param(
    [string]$DataDir = $(if ($env:BUDGET_DATA_DIR) { $env:BUDGET_DATA_DIR } else { 'C:\BudgetData' }),
    [string]$BackupDir = '',
    [string]$ServiceUser = 'budgetsvc',
    [string]$TaskName = 'Household Budget',
    [int]$Port = 8765,
    [switch]$ShowUserOnSignInScreen
)

$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $PSScriptRoot

function Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Ok([string]$Message) { Write-Host "    OK    $Message" -ForegroundColor Green }
function Note([string]$Message) { Write-Host "    NOTE  $Message" -ForegroundColor Yellow }
function Stop-Install([string]$Message) {
    Write-Host "`n    STOPPED: $Message" -ForegroundColor Red
    Write-Host '    Nothing else was changed. Fix the above and run this script again.' -ForegroundColor Red
    exit 1
}
function ConvertTo-Plain([Security.SecureString]$Secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
function Test-Listening([int]$P) {
    return [bool](Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue)
}

# --------------------------------------------------------------------------- 0. checks
Step 'Checking prerequisites'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Stop-Install 'This must run as administrator. Right-click Windows PowerShell > Run as administrator.'
}
Ok 'Running as administrator'

$Python = Join-Path $RepoDir '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { Stop-Install "Virtual environment not found at $Python." }
$PyHome = (Get-Content (Join-Path $RepoDir '.venv\pyvenv.cfg') |
    Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1) -replace '^\s*home\s*=\s*', ''
if (-not (Test-Path $PyHome)) { Stop-Install "Python install folder not found: $PyHome" }
Ok "Python: $PyHome"

if (-not (Test-Path (Join-Path $DataDir 'budget.db'))) { Stop-Install "No database at $DataDir\budget.db." }
Ok "Data folder: $DataDir"

# The backup folder comes from [backup] dir in the private config.toml.
if (-not $BackupDir) {
    $env:BUDGET_DATA_DIR = $DataDir
    $BackupDir = "$(& $Python -c 'from budget.config import load_config; print(load_config().backup_dir or str())')".Trim()
}
if (-not $BackupDir) { Stop-Install "Set [backup] dir in $DataDir\config.toml." }
$BackupDrive = Split-Path -Qualifier $BackupDir
if (-not (Test-Path "$BackupDrive\")) {
    Stop-Install "Backup drive $BackupDrive is not connected. Plug in the backup drive (it must show as $BackupDrive)."
}
Ok "Backup drive $BackupDrive is connected"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Note "Task '$TaskName' already exists; stopping it so it can be updated."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}
if (Test-Listening $Port) {
    Stop-Install ("Something is already using port $Port, probably the app running in a terminal. " +
        "Close that terminal tab (or press Ctrl+C in it) and run this script again.")
}
Ok "Port $Port is free"

# --------------------------------------------------------------------------- 1. account
Step "Setting up the standard (non-admin) account '$ServiceUser'"
$user = Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue
if ($user) {
    Note "Account '$ServiceUser' already exists. Enter its current password."
    $secure = Read-Host -AsSecureString "Password for $ServiceUser"
} else {
    Write-Host '    Choose a strong password and save it in your password manager.'
    Write-Host '    Nobody signs in with this account; it only runs the app.'
    $secure = Read-Host -AsSecureString "New password for $ServiceUser"
    $again = Read-Host -AsSecureString 'Type it again'
    if ((ConvertTo-Plain $secure) -cne (ConvertTo-Plain $again)) { Stop-Install 'The two passwords did not match.' }
    if ((ConvertTo-Plain $secure).Length -lt 12) { Stop-Install 'Please use at least 12 characters.' }
    New-LocalUser -Name $ServiceUser -Password $secure -FullName 'Household Budget service' `
        -Description 'Runs the Household Budget app (not an admin)' `
        -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires | Out-Null
    Ok "Created account '$ServiceUser'"
}
# Standard user: member of Users (by SID, works in any Windows language), never Administrators.
Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $ServiceUser -ErrorAction SilentlyContinue
$admins = Get-LocalGroupMember -SID 'S-1-5-32-544' | Where-Object { $_.Name -like "*\$ServiceUser" }
if ($admins) { Stop-Install "'$ServiceUser' is an administrator. Remove it from Administrators first." }
Ok "'$ServiceUser' is a standard user"

$userListKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList'
if (-not $ShowUserOnSignInScreen) {
    New-Item -Path $userListKey -Force | Out-Null
    New-ItemProperty -Path $userListKey -Name $ServiceUser -Value 0 -PropertyType DWord -Force | Out-Null
    Ok "Hidden from the sign-in screen"
}

# --------------------------------------------------------------------------- 2. batch logon right
Step "Allowing '$ServiceUser' to run scheduled tasks (Log on as a batch job)"
$sid = (New-Object Security.Principal.NTAccount($ServiceUser)).Translate([Security.Principal.SecurityIdentifier]).Value
$inf = Join-Path $env:TEMP ("hb-rights-{0}.inf" -f [guid]::NewGuid())
$sdb = Join-Path $env:TEMP ("hb-rights-{0}.sdb" -f [guid]::NewGuid())
try {
    secedit /export /cfg $inf /areas USER_RIGHTS | Out-Null
    $lines = @(Get-Content $inf)
    $idx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match '^\s*SeBatchLogonRight\s*=') { $idx = $i } }
    if ($idx -ge 0 -and $lines[$idx] -match ([regex]::Escape("*$sid") + '(?![\d-])')) {
        Ok 'Already allowed'
    } else {
        if ($idx -ge 0) {
            $lines[$idx] = $lines[$idx].TrimEnd() + ",*$sid"
        } else {
            $at = [array]::IndexOf($lines, '[Privilege Rights]')
            if ($at -lt 0) { Stop-Install 'Could not read the local security policy.' }
            $before = @($lines[0..$at])
            $after = if ($at -lt $lines.Count - 1) { @($lines[($at + 1)..($lines.Count - 1)]) } else { @() }
            $lines = $before + @("SeBatchLogonRight = *$sid") + $after
        }
        Set-Content -Path $inf -Value $lines -Encoding Unicode
        secedit /configure /db $sdb /cfg $inf /areas USER_RIGHTS | Out-Null
        if ($LASTEXITCODE -ne 0) { Stop-Install 'Windows refused the "Log on as a batch job" change.' }
        Ok 'Allowed'
    }
} finally {
    Remove-Item $inf, $sdb, ($sdb -replace '\.sdb$', '.jfm') -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------------- 3. folder permissions
Step 'Setting folder permissions (least privilege)'
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir 'logs') | Out-Null
$grants = @(
    @{ Path = $DataDir;   Right = '(OI)(CI)M';  Why = 'modify (database, logs)' },
    @{ Path = $BackupDir; Right = '(OI)(CI)M';  Why = 'modify (backups)' },
    @{ Path = $RepoDir;   Right = '(OI)(CI)RX'; Why = 'read and run (the code)' },
    @{ Path = $PyHome;    Right = '(OI)(CI)RX'; Why = 'read and run (Python)' }
)
foreach ($g in $grants) {
    icacls $g.Path /grant "${ServiceUser}:$($g.Right)" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Install "Could not set permissions on $($g.Path)." }
    Ok "$($g.Path): $($g.Why)"
}
if ($DataDir -ne 'C:\BudgetData') {
    [Environment]::SetEnvironmentVariable('BUDGET_DATA_DIR', $DataDir, 'Machine')
    Ok "BUDGET_DATA_DIR set to $DataDir for all users"
}

# --------------------------------------------------------------------------- 4. power
Step 'Power settings (plugged in): never sleep, screen off after 10 minutes'
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10
Ok 'Done. Lock the PC or sign out instead of shutting it down.'

# --------------------------------------------------------------------------- 5. scheduled task
Step "Registering scheduled task '$TaskName'"
$action = New-ScheduledTaskAction -Execute $Python -Argument '-m budget' -WorkingDirectory $RepoDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$plain = ConvertTo-Plain $secure
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User "$env:COMPUTERNAME\$ServiceUser" -Password $plain -RunLevel Limited -Force `
        -Description 'Household Budget web app on 127.0.0.1:8765 (reached from phones via Tailscale).' | Out-Null
} catch {
    Stop-Install "Could not register the task. If '$ServiceUser' already existed, the password may be wrong. ($($_.Exception.Message))"
} finally {
    $plain = $null
}
Ok 'Runs at startup, whether or not anyone is signed in; restarts on failure (3 x 1 min)'

# --------------------------------------------------------------------------- 6. first backup, start, verify
Step "First backup to $BackupDir"
$env:BUDGET_DATA_DIR = $DataDir
& $Python -m budget backup
if ($LASTEXITCODE -ne 0) { Note 'The first backup failed (see above). The app will retry and show a banner.' }
else { Ok 'Backup written' }

Step 'Starting the app'
Start-ScheduledTask -TaskName $TaskName
$healthy = $false
for ($i = 0; $i -lt 30 -and -not $healthy; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        $healthy = ($r.status -eq 'ok')
    } catch { }
}
if (-not $healthy) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Stop-Install ("The app did not answer. Task result code: $($info.LastTaskResult). " +
        "Check $DataDir\logs\app.log and Task Scheduler > '$TaskName' > History.")
}
$owner = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*-m budget*' } |
    ForEach-Object { (Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User } | Select-Object -First 1
Ok "App is up at http://localhost:$Port (running as: $owner)"
if ($r.backup.ok) { Ok "Backups OK (last: $($r.backup.last_ok_at))" } else { Note "Backup status: $($r.backup.problem)" }

Write-Host "`nAll set. Next: restart the PC and DON'T sign in for 2 minutes. Then sign in and run:" -ForegroundColor Cyan
Write-Host '    powershell -ExecutionPolicy Bypass -File .\scripts\check_service.ps1' -ForegroundColor Cyan
