@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PYFILE=%SCRIPT_DIR%dm4_align_export_gui.py"
set "PYEXE=D:\Anaconda\envs\abtem\python.exe"

if not exist "%PYEXE%" (
    echo Python not found:
    echo %PYEXE%
    pause
    exit /b 1
)

if not exist "%PYFILE%" (
    echo Script not found:
    echo %PYFILE%
    pause
    exit /b 1
)

"%PYEXE%" "%PYFILE%"

echo.
echo Program finished. Press any key to close.
pause >nul