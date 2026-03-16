@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "APP_NAME=DM4AlignExportGUI"
set "PYEXE=%SCRIPT_DIR%packvenv\Scripts\python.exe"
set "MAIN_PY=%SCRIPT_DIR%dm4_align_export_gui.py"
set "DIST_ROOT=%SCRIPT_DIR%dist"
set "DIST_DIR=%DIST_ROOT%\%APP_NAME%"
set "SPEC_FILE=%SCRIPT_DIR%%APP_NAME%.spec"
set "RELEASE_README=%DIST_DIR%\README.txt"

cd /d "%SCRIPT_DIR%"

if not exist "%PYEXE%" (
    echo [ERROR] Python not found:
    echo %PYEXE%
    echo.
    echo Create the build environment first, for example:
    echo   python -m venv packvenv
    echo   packvenv\Scripts\python -m pip install -U pip
    echo   packvenv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "%MAIN_PY%" (
    echo [ERROR] Main script not found:
    echo %MAIN_PY%
    pause
    exit /b 1
)

set "PYBASE_FILE=%TEMP%\%APP_NAME%_pybase.txt"
if exist "%PYBASE_FILE%" del /q "%PYBASE_FILE%"
"%PYEXE%" -c "import sys, pathlib; pathlib.Path(r'%PYBASE_FILE%').write_text(sys.base_prefix, encoding='utf-8')"
if exist "%PYBASE_FILE%" (
    set /p PYBASE=<"%PYBASE_FILE%"
    del /q "%PYBASE_FILE%"
)

if not defined PYBASE (
    echo [ERROR] Failed to detect Python base prefix.
    pause
    exit /b 1
)

echo [INFO] Build Python: %PYEXE%
echo [INFO] PYBASE=%PYBASE%

if exist build rmdir /s /q build
if exist "%DIST_ROOT%" rmdir /s /q "%DIST_ROOT%"
if exist "%SPEC_FILE%" del /q "%SPEC_FILE%"

echo [INFO] Running PyInstaller...
if exist "%SCRIPT_DIR%hooks" (
    echo [INFO] Using extra hooks from %SCRIPT_DIR%hooks
    "%PYEXE%" -m PyInstaller ^
      --noconfirm ^
      --clean ^
      --onedir ^
      --windowed ^
      --name %APP_NAME% ^
      --additional-hooks-dir "%SCRIPT_DIR%hooks" ^
      --exclude-module IPython ^
      --exclude-module jupyter ^
      --exclude-module notebook ^
      --exclude-module nbformat ^
      --exclude-module nbconvert ^
      --exclude-module pandas ^
      --exclude-module sqlalchemy ^
      --exclude-module zarr ^
      --exclude-module numba ^
      --exclude-module llvmlite ^
      --exclude-module PySide2 ^
      --exclude-module PySide6 ^
      --exclude-module PyQt5 ^
      --exclude-module PyQt6 ^
      --exclude-module pkg_resources ^
      --exclude-module setuptools ^
      --hidden-import tkinter ^
      --hidden-import tkinter.filedialog ^
      --hidden-import tkinter.messagebox ^
      --hidden-import tkinter.ttk ^
      --hidden-import dask ^
      --hidden-import dask.array ^
      --hidden-import hyperspy.io ^
      --hidden-import scipy.io.matlab ^
      --collect-data hyperspy ^
      --collect-data rsciio ^
      --collect-submodules dask ^
      --collect-submodules hyperspy ^
      --collect-submodules rsciio ^
      --collect-all PIL ^
      --collect-data matplotlib ^
      --collect-binaries matplotlib ^
      "%MAIN_PY%"
) else (
    echo [INFO] No custom hooks directory found. Using PyInstaller defaults only.
    "%PYEXE%" -m PyInstaller ^
      --noconfirm ^
      --clean ^
      --onedir ^
      --windowed ^
      --name %APP_NAME% ^
      --exclude-module IPython ^
      --exclude-module jupyter ^
      --exclude-module notebook ^
      --exclude-module nbformat ^
      --exclude-module nbconvert ^
      --exclude-module pandas ^
      --exclude-module sqlalchemy ^
      --exclude-module zarr ^
      --exclude-module numba ^
      --exclude-module llvmlite ^
      --exclude-module PySide2 ^
      --exclude-module PySide6 ^
      --exclude-module PyQt5 ^
      --exclude-module PyQt6 ^
      --exclude-module pkg_resources ^
      --exclude-module setuptools ^
      --hidden-import tkinter ^
      --hidden-import tkinter.filedialog ^
      --hidden-import tkinter.messagebox ^
      --hidden-import tkinter.ttk ^
      --hidden-import dask ^
      --hidden-import dask.array ^
      --hidden-import hyperspy.io ^
      --hidden-import scipy.io.matlab ^
      --collect-data hyperspy ^
      --collect-data rsciio ^
      --collect-submodules dask ^
      --collect-submodules hyperspy ^
      --collect-submodules rsciio ^
      --collect-all PIL ^
      --collect-data matplotlib ^
      --collect-binaries matplotlib ^
      "%MAIN_PY%"
)

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

if not exist "%DIST_DIR%" (
    echo [ERROR] Build finished but output folder is missing:
    echo %DIST_DIR%
    pause
    exit /b 1
)

set "INTERNAL_DIR=%DIST_DIR%\_internal"
if exist "%INTERNAL_DIR%" (
    echo [INFO] Copying runtime DLLs to:
    echo %INTERNAL_DIR%

    if exist "%PYBASE%\DLLs" (
        copy /y "%PYBASE%\DLLs\*.dll" "%INTERNAL_DIR%\" >nul 2>nul
    )

    if exist "%PYBASE%\Library\bin" (
        copy /y "%PYBASE%\Library\bin\*.dll" "%INTERNAL_DIR%\" >nul 2>nul
    )

    copy /y "%PYBASE%\*.dll" "%INTERNAL_DIR%\" >nul 2>nul
)

for %%F in ("%SCRIPT_DIR%README*.txt") do (
    if exist "%%~fF" (
        copy /y "%%~fF" "%RELEASE_README%" >nul
        goto :readme_done
    )
)
:readme_done

(
    echo %APP_NAME% release package
    echo.
    echo Run:
    echo   %APP_NAME%.exe
    echo.
    echo Notes:
    echo - Keep the whole folder together when sending to another user.
    echo - Do not move the exe out of this folder by itself.
    echo - If Windows SmartScreen appears, choose "More info" then "Run anyway" after verifying the source.
)> "%DIST_DIR%\RUN_ME.txt"

if exist "%DIST_ROOT%\%APP_NAME%.zip" del /q "%DIST_ROOT%\%APP_NAME%.zip"
powershell -NoProfile -Command "Compress-Archive -Path '%DIST_DIR%\*' -DestinationPath '%DIST_ROOT%\%APP_NAME%.zip' -Force" >nul 2>nul
if errorlevel 1 (
    echo [WARN] ZIP packaging failed. The folder build is still usable.
) else (
    echo [INFO] ZIP package created:
    echo %DIST_ROOT%\%APP_NAME%.zip
)

echo.
echo [OK] Build finished.
echo Folder package:
echo %DIST_DIR%
echo.
echo Share the whole folder above, or the ZIP package if it was created.
pause
