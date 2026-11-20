@echo off
setlocal enabledelayedexpansion

title MetaCT - Installer
color 0A

echo ============================================
echo    MetaCT - Installer
echo ============================================
echo.

cd /d "%~dp0"

:: Find Python executable (avoiding Windows Store aliases)
echo [1/5] Checking Python installation...

set "PYTHON_EXE="

:: Try py launcher first (most reliable on Windows)
where py >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
)

:: If py launcher didn't work, try common Python locations
if not defined PYTHON_EXE (
    for %%p in (
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
        "C:\Python39\python.exe"
        "%PROGRAMFILES%\Python312\python.exe"
        "%PROGRAMFILES%\Python311\python.exe"
        "%PROGRAMFILES%\Python310\python.exe"
    ) do (
        if exist %%p (
            set "PYTHON_EXE=%%~p"
            goto :found_python
        )
    )
)

:found_python
if not defined PYTHON_EXE (
    echo.
    echo ERROR: Python not found!
    echo.
    echo Please install Python 3.9+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    echo IMPORTANT: Disable Windows Store Python aliases:
    echo   Settings ^> Apps ^> Advanced app settings ^> App execution aliases
    echo   Turn OFF "python.exe" and "python3.exe"
    echo.
    pause
    exit /b 1
)

echo    Found Python: %PYTHON_EXE%

:: Verify Python version
for /f "tokens=*" %%i in ('"%PYTHON_EXE%" --version 2^>^&1') do set "PYVER_FULL=%%i"
echo    Version: %PYVER_FULL%

:: Verify it's real Python (not MS Store stub)
"%PYTHON_EXE%" -c "import sys; exit(0 if sys.version_info >= (3, 9) else 1)" 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Python 3.9+ required
    pause
    exit /b 1
)
echo    Python version OK
echo.

:: Check write permissions
echo [2/5] Creating virtual environment...
echo test > "%~dp0.write_test" 2>nul
if %errorlevel% neq 0 (
    echo.
    echo ERROR: No write permission in this folder!
    echo.
    echo Solutions:
    echo   1. Move mt5copy folder to: C:\Users\%USERNAME%\mt5copy
    echo   2. Or right-click install.bat and "Run as administrator"
    echo.
    pause
    exit /b 1
)
del "%~dp0.write_test" 2>nul

:: Create virtual environment
if exist "venv\Scripts\python.exe" (
    echo    Virtual environment already exists, skipping...
) else (
    if exist "venv" rmdir /s /q venv 2>nul
    "%PYTHON_EXE%" -m venv venv
    if not exist "venv\Scripts\python.exe" (
        echo.
        echo ERROR: Failed to create virtual environment
        echo.
        echo If in Program Files, move folder to: C:\Users\%USERNAME%\mt5copy
        echo Or run as administrator.
        echo.
        pause
        exit /b 1
    )
    echo    Virtual environment created
)
echo.

:: Install packages using venv Python directly
echo [3/5] Installing dependencies...
set "VENV_PYTHON=%~dp0venv\Scripts\python.exe"
set "VENV_PIP=%~dp0venv\Scripts\pip.exe"

:: Upgrade pip first
"%VENV_PYTHON%" -m pip install --upgrade pip --quiet 2>nul
if %errorlevel% neq 0 (
    echo    Warning: Could not upgrade pip, continuing...
)

:: Install required packages
"%VENV_PIP%" install MetaTrader5 PyYAML PyQt6 pytest --quiet
if %errorlevel% neq 0 (
    :: Try with python -m pip as fallback
    "%VENV_PYTHON%" -m pip install MetaTrader5 PyYAML PyQt6 pytest
    if %errorlevel% neq 0 (
        echo ERROR: Failed to install dependencies
        echo.
        echo Try running manually:
        echo   venv\Scripts\pip.exe install MetaTrader5 PyYAML PyQt6 pytest
        pause
        exit /b 1
    )
)
echo    Dependencies installed:
echo      - MetaTrader5
echo      - PyYAML
echo      - PyQt6
echo      - pytest
echo.

:: Check if config already exists
echo [4/5] Setting up configuration...
if not exist "config" mkdir config
if not exist "config\config.yaml" (
    echo    Creating default config.yaml...
    (
        echo # MetaCT Configuration
        echo.
        echo master:
        echo   poll_interval_ms: 500
        echo.
        echo slaves:
        echo   - name: "Slave1"
        echo     login: 12345678
        echo     password: "your_password"
        echo     server: "Broker-Server"
        echo     multiplier: 1.0
        echo     adjust_by_leverage: true
        echo     enabled: true
        echo.
        echo symbol_mapping: {}
        echo.
        echo settings:
        echo   sync_existing_on_start: true
        echo   max_slippage_points: 0
        echo   retry_attempts: 3
        echo   retry_delay_ms: 1000
        echo   log_level: INFO
    ) > config\config.yaml
    echo    Default config created
) else (
    echo    config.yaml already exists, skipping...
)
echo.

:: Create state and logs directories
echo [5/5] Finalizing setup...
if not exist "state" mkdir state
if not exist "logs" mkdir logs
if not exist "traces" mkdir traces
if not exist "dumps" mkdir dumps
echo    State, logs, traces, and dumps directories ready
echo.

echo ============================================
echo    Installation Complete!
echo ============================================
echo.
echo Next steps:
echo   1. Edit config\config.yaml with your account credentials
echo   2. Open MT5 terminal and login to your MASTER account
echo   3. Run run.bat to start the copy trading service
echo.
pause
