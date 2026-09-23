@echo off
setlocal enabledelayedexpansion
title ClassTrack - Build Desktop Installer

echo ============================================================
echo      ClassTrack Desktop Camera Node - Installer Builder
echo ============================================================
echo.

cd /d "%~dp0"

:: 1. Check Python virtual environment
set "PYTHON_EXE=desktop-app\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment not found at %PYTHON_EXE%
    echo Please make sure desktop-app/.venv is set up.
    pause
    exit /b 1
)

:: Private runtime configuration is included in the installer, never in the source ZIP.
if not exist "desktop-app\.env" (
    echo [ERROR] desktop-app\.env is missing. Add the private Render EDGE_API_KEY before building.
    exit /b 1
)
findstr /R /C:"^[ ]*EDGE_API_KEY=." "desktop-app\.env" >nul
if errorlevel 1 (
    echo [ERROR] EDGE_API_KEY is empty in desktop-app\.env. The installer cannot authenticate with Render.
    exit /b 1
)
:: 2. Ensure PyInstaller is installed
echo [1/3] Verifying PyInstaller...
"%PYTHON_EXE%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing now...
    "%PYTHON_EXE%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Could not install PyInstaller.
        exit /b 1
    )
)

set "TEMP_BUILD=%LOCALAPPDATA%\ClassTrackBuild"
if not exist "%TEMP_BUILD%" mkdir "%TEMP_BUILD%"
if not exist "dist\installer" mkdir "dist\installer"

:: 3. Run PyInstaller build
echo.
echo [2/3] Building native Windows application with PyInstaller...
echo       (Building in %TEMP_BUILD% to avoid OneDrive lock issues)
echo.
"%PYTHON_EXE%" -m PyInstaller desktop-app\ClassTrack.spec --noconfirm --distpath "%TEMP_BUILD%\dist" --workpath "%TEMP_BUILD%\build"

if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed! Please check the output above.
    pause
    exit /b 1
)

:: Ensure models and assets are populated in distribution
if not exist "%TEMP_BUILD%\dist\ClassTrack\models" mkdir "%TEMP_BUILD%\dist\ClassTrack\models"
xcopy /E /I /Y "desktop-app\models" "%TEMP_BUILD%\dist\ClassTrack\models" >nul 2>&1
if not exist "%TEMP_BUILD%\dist\ClassTrack\assets" mkdir "%TEMP_BUILD%\dist\ClassTrack\assets"
xcopy /E /I /Y "desktop-app\assets" "%TEMP_BUILD%\dist\ClassTrack\assets" >nul 2>&1
copy /Y "desktop-app\.env" "%TEMP_BUILD%\dist\ClassTrack\.env" >nul
if errorlevel 1 (
    echo [ERROR] Could not add the private runtime configuration to the installer build.
    exit /b 1
)

:: 4. Locate Inno Setup Compiler (ISCC.exe)
echo.
echo [3/3] Compiling standalone Windows Setup Wizard with Inno Setup...
set "ISCC_EXE="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC_EXE=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC_EXE=C:\Program Files\Inno Setup 6\ISCC.exe"

if not defined ISCC_EXE (
    where iscc >nul 2>&1
    if not errorlevel 1 set "ISCC_EXE=iscc"
)

if not defined ISCC_EXE (
    echo [ERROR] Inno Setup compiler ISCC.exe was not found.
    echo Install Inno Setup 6 and run BUILD_INSTALLER.bat again.
    exit /b 1
)

"%ISCC_EXE%" /DAppSourceDir="%TEMP_BUILD%\dist\ClassTrack" /O"%CD%\dist\installer" desktop-app\installer.iss

if errorlevel 1 (
    echo.
    echo [ERROR] Inno Setup compilation failed!
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   SUCCESS! Standalone Installer Generated:
echo   dist\installer\ClassTrack_Setup_v1.0.exe
echo ============================================================
echo.
pause
