@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul && (
  start "CampusNet" /b pythonw "%~dp0campusnet.py"
) || (
  start "CampusNet" /b python "%~dp0campusnet.py"
)
