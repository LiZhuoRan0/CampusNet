$ErrorActionPreference = 'Stop'
$startupPath = [Environment]::GetFolderPath('Startup')
Remove-Item -LiteralPath (Join-Path $startupPath 'CampusNetAutoLogin.vbs') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $startupPath 'CampusNetAutoLogin.cmd') -Force -ErrorAction SilentlyContinue
Write-Host 'Removed CampusNetAutoLogin Startup launcher.'
