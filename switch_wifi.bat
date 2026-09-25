@echo off
setlocal
if "%~1"=="" (
  echo Usage: switch_wifi.bat BIT-Web ^| BIT-Mobile
  exit /b 2
)
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
python "%~dp0campusnet.py" --switch "%~1"
exit /b %errorlevel%
