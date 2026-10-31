@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo ========================================
echo MyfansDownloader
echo ========================================
echo.

call :find_python
if errorlevel 1 (
    echo ERROR: Python 3.9 or newer was not found.
    echo.
    echo The "python" command on this PC is only the Microsoft Store alias.
    echo Run SETUP.bat once, then double-click this file again.
    echo.
    pause
    exit /b 1
)

echo Opening MyfansDownloader...
echo.

"%PY_EXE%" %PY_ARGS% "%~dp0MyfansDownloader_unified.py" web
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo Program stopped with exit code %EXITCODE%.
) else (
    echo Program finished.
)
pause
exit /b %EXITCODE%

:find_python
set "PY_EXE="
set "PY_ARGS="

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
    exit /b 0
)

for /f "delims=" %%I in ('where python 2^>nul') do (
    echo %%I | findstr /i /c:"\WindowsApps\" >nul
    if errorlevel 1 (
        "%%I" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
        if not errorlevel 1 (
            set "PY_EXE=%%I"
            set "PY_ARGS="
            exit /b 0
        )
    )
)

for /f "delims=" %%I in ('where python3 2^>nul') do (
    echo %%I | findstr /i /c:"\WindowsApps\" >nul
    if errorlevel 1 (
        "%%I" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
        if not errorlevel 1 (
            set "PY_EXE=%%I"
            set "PY_ARGS="
            exit /b 0
        )
    )
)

exit /b 1
