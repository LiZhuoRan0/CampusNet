$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath
    )
    exit $process.ExitCode
}

$taskName = 'CampusNetAutoLogin'
$scriptPath = Join-Path $PSScriptRoot 'campusnet.py'
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object {
    $_.CommandLine -like "*$scriptPath*"
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
}
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
$startupPath = [Environment]::GetFolderPath('Startup')
Remove-Item -LiteralPath (Join-Path $startupPath 'CampusNetAutoLogin.vbs') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $startupPath 'CampusNetAutoLogin.cmd') -Force -ErrorAction SilentlyContinue
Write-Host 'Removed CampusNetAutoLogin scheduled task and legacy Startup launcher.'
