# Installs a hidden launcher in the current user's Windows Startup folder.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'start.bat'
$startupPath = [Environment]::GetFolderPath('Startup')
$launcherPath = Join-Path $startupPath 'CampusNetAutoLogin.vbs'
$legacyLauncherPath = Join-Path $startupPath 'CampusNetAutoLogin.cmd'

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Start script not found: $scriptPath"
}

$launcher = @"
Set shell = CreateObject("WScript.Shell")
shell.Run "cmd.exe /c ""$scriptPath""", 0, False
"@
Set-Content -LiteralPath $launcherPath -Value $launcher -Encoding ASCII
Remove-Item -LiteralPath $legacyLauncherPath -Force -ErrorAction SilentlyContinue
Write-Host "Installed Startup launcher: $launcherPath"
Write-Host 'The password is protected with Windows DPAPI after the first run.'
