@echo off
REM VIVER - LabVIEW version control launcher (Windows)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: creating the virtual environment...
    python -m venv .venv || (echo Python not found. Install Python 3.9+ from https://www.python.org & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
)
start "" http://127.0.0.1:5000
".venv\Scripts\python.exe" app.py
pause
