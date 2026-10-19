@echo off
REM FormFlow launcher (Phase 1). Local-only, single worker.
setlocal
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python 3.11+ not found on PATH. Install from https://www.python.org/downloads/
  exit /b 1
)

if exist .venv311\Scripts\python.exe (
  call .venv311\Scripts\activate.bat
) else (
  if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 exit /b 1
  )
  call .venv\Scripts\activate.bat
  python -m pip install -q -r requirements.txt
  if errorlevel 1 (
    echo ERROR: pip install failed. Check your network and Python version.
    exit /b 1
  )
)

python run.py
