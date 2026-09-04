# Installs a highest-privilege, hidden task for the current Windows user.
# This one-time UAC prompt lets the recovery code reset the Wi-Fi adapter when
# Windows refuses a normal user's request.  The task itself uses pythonw, so
# no console window appears during normal background operation.
$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdministrator) {
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath
    )
    exit $process.ExitCode
}

$taskName = 'CampusNetAutoLogin'
$scriptPath = Join-Path $PSScriptRoot 'campusnet.py'
$pythonw = Get-Command pythonw.exe -ErrorAction Stop | Select-Object -ExpandProperty Source
$userId = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "CampusNet program not found: $scriptPath"
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $scriptPath) -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Description 'BIT-Web 自动检测、认证与恢复' -Force | Out-Null

# Replace the older Startup-folder launcher, and replace any already-running
# non-elevated instance so this installation is effective immediately.
$startupPath = [Environment]::GetFolderPath('Startup')
Remove-Item -LiteralPath (Join-Path $startupPath 'CampusNetAutoLogin.vbs') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $startupPath 'CampusNetAutoLogin.cmd') -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object {
    $_.CommandLine -like "*$scriptPath*"
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
}
Start-ScheduledTask -TaskName $taskName

Write-Host "Installed and started highest-privilege scheduled task: $taskName"
Write-Host 'Windows will request UAC permission only during this installation.'
