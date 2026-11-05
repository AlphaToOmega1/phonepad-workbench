@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" -B "%~dp0launch.py"
  exit /b
)
py -3 "%~dp0launch.py"
if errorlevel 1 pause
