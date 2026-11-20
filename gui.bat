@echo off
cd /d "%~dp0"

:: Use pythonw.exe (windowless) to avoid console window
set "VENV_PYTHONW=%~dp0venv\Scripts\pythonw.exe"

if not exist "%VENV_PYTHONW%" (
    echo ERROR: Virtual environment not found.
    echo Please run install.bat first.
    pause
    exit /b 1
)

:: Start detached so this batch window closes immediately
start "" "%VENV_PYTHONW%" -m src.gui_main
