@echo off
setlocal

set "SRC=C:\Mac\Home\Documents\workspace\revisions\private.2023\tips\mt5copy"
set "DST=C:\mt5copy"

echo Deploying %SRC% -^> %DST%
echo.

rem --- Source files (everything under src\) ---
echo [1/2] Refreshing src\
rmdir /S /Q "%DST%\src" 2>nul
xcopy /E /Y /I "%SRC%\src" "%DST%\src" >nul
if errorlevel 1 (
    echo ERROR: xcopy src failed
    pause
    exit /b 1
)

rem --- .bat files at repo root, EXCLUDING the running deploy.bat ---
rem    deploy.bat is updated last via a detached helper so the currently
rem    executing instance can exit first.
echo [2/2] Refreshing .bat files
for %%F in ("%SRC%\*.bat") do (
    if /I not "%%~nxF"=="deploy.bat" (
        copy /Y "%%F" "%DST%\%%~nxF" >nul
        if errorlevel 1 (
            echo ERROR: copy %%~nxF failed
            pause
            exit /b 1
        )
        echo   - %%~nxF
    )
)

rem --- Self-update deploy.bat via detached helper ---
rem We can't safely overwrite the running deploy.bat inline. Build a tiny
rem helper bat in %TEMP%, launch it detached, then exit. The helper waits
rem 2s (long enough for this script to finish), copies the new deploy.bat
rem into place, and deletes itself.
echo   - deploy.bat (scheduled self-update)
set "HELPER=%TEMP%\mt5copy_selfupdate.bat"
> "%HELPER%" echo @echo off
>>"%HELPER%" echo timeout /t 2 /nobreak ^>nul
>>"%HELPER%" echo copy /Y "%SRC%\deploy.bat" "%DST%\deploy.bat" ^>nul
>>"%HELPER%" echo del "%%~f0"
start "" /B cmd /c "%HELPER%"

echo.
echo Done.
endlocal
