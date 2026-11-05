@echo off
setlocal
cd /d "%~dp0"
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
if exist "%~dp0..\.venv\Scripts\python.exe" (
  "%~dp0..\.venv\Scripts\python.exe" -B "%~dp0dictation.py"
) else (
  py -3 "%~dp0dictation.py"
)
if errorlevel 1 pause
