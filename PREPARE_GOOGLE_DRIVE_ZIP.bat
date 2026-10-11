@echo off
setlocal enabledelayedexpansion
title ClassTrack - Prepare Google Drive Package

echo ============================================================
echo   ClassTrack — Google Drive Package Generator
echo ============================================================
echo.

cd /d "%~dp0"

set "INSTALLER_EXE=dist\installer\ClassTrack_Setup_v1.0.exe"
if not exist "%INSTALLER_EXE%" (
    echo [ERROR] Existing standalone installer not found at: %INSTALLER_EXE%
    echo This package job preserves the existing installer and will not rebuild it.
    exit /b 1
)

set "PACKAGE_DIR=%~dp0dist\GoogleDrive_Package"
set "ZIP_FILE=%~dp0dist\ClassTrack_GoogleDrive_Package.zip"

echo [1/5] Preparing clean distribution directory...
if exist "%PACKAGE_DIR%" rmdir /s /q "%PACKAGE_DIR%"
if exist "%ZIP_FILE%" del /f /q "%ZIP_FILE%"
mkdir "%PACKAGE_DIR%"
mkdir "%PACKAGE_DIR%\Source_Code"

echo [2/5] Adding non-tech user essentials...
if exist "START_HERE.html" copy "START_HERE.html" "%PACKAGE_DIR%\" >nul
if exist "%PACKAGE_DIR%\START_HERE.html" powershell -NoProfile -Command "$p=Join-Path $env:PACKAGE_DIR 'START_HERE.html'; $s=[IO.File]::ReadAllText($p); $s=$s.Replace('dist/installer/ClassTrack_Setup_v1.0.exe','ClassTrack_Setup_v1.0.exe'); [IO.File]::WriteAllText($p,$s)"
if exist "Open_ClassTrack_Dashboard.url" copy "Open_ClassTrack_Dashboard.url" "%PACKAGE_DIR%\" >nul
if exist "README_NON_TECH.txt" copy "README_NON_TECH.txt" "%PACKAGE_DIR%\" >nul

if exist "%INSTALLER_EXE%" (
    echo       Copying ClassTrack_Setup_v1.0.exe standalone installer...
    copy /Y "%INSTALLER_EXE%" "%PACKAGE_DIR%\" >nul
)

echo [3/5] Copying clean source code (excluding heavy .venv and temp files)...
copy "START_ALL.bat" "%PACKAGE_DIR%\Source_Code\" >nul
if exist "README.md" copy "README.md" "%PACKAGE_DIR%\Source_Code\" >nul
if exist "handout.md" copy "handout.md" "%PACKAGE_DIR%\Source_Code\" >nul
if exist "BUILD_INSTALLER.bat" copy "BUILD_INSTALLER.bat" "%PACKAGE_DIR%\Source_Code\" >nul

:: Copy desktop-app without .venv or __pycache__
robocopy "desktop-app" "%PACKAGE_DIR%\Source_Code\desktop-app" /E /XD ".venv" "__pycache__" ".pytest_cache" "uploads" /XF "*.pyc" "*.log" "*.db*" "*.sqlite*" ".env" ".env.local" ".env.production" ".env.development" ".env.test" >nul
if errorlevel 8 (
    echo [ERROR] Could not copy desktop-app source files.
    exit /b 1
)

:: Copy web-dashboard without .venv or __pycache__
robocopy "web-dashboard" "%PACKAGE_DIR%\Source_Code\web-dashboard" /E /XD ".venv" "__pycache__" ".pytest_cache" "uploads" /XF "*.pyc" "*.log" "*.db*" "*.sqlite*" ".env" ".env.local" ".env.production" ".env.development" ".env.test" >nul
if errorlevel 8 (
    echo [ERROR] Could not copy web-dashboard source files.
    exit /b 1
)

:: Remove local secrets defensively while keeping safe .env.example templates.
powershell -NoProfile -Command "$root=[IO.Path]::GetFullPath('%PACKAGE_DIR%\Source_Code'); $expected=[IO.Path]::GetFullPath('%~dp0dist\GoogleDrive_Package\Source_Code'); if ($root -ne $expected) { throw 'Unexpected source package path' }; Get-ChildItem -LiteralPath $root -Recurse -Force -File | Where-Object { $_.Name -in @('.env','.env.local','.env.production','.env.development','.env.test') } | Remove-Item -Force"
if errorlevel 1 (
    echo [ERROR] Could not sanitize environment files from the source package.
    exit /b 1
)

echo [4/5] Creating lightweight Source_Code.zip (for evaluators/developers)...
powershell -NoProfile -Command "Compress-Archive -Path '%PACKAGE_DIR%\Source_Code\*' -DestinationPath '%PACKAGE_DIR%\ClassTrack_Source_Code.zip' -Force"
if errorlevel 1 (
    echo [ERROR] Could not create ClassTrack_Source_Code.zip.
    exit /b 1
)

echo [5/5] Creating complete Google Drive ZIP with the unchanged installer...
tar -a -c -f "%ZIP_FILE%" -C "%PACKAGE_DIR%" .
if errorlevel 1 (
    echo [ERROR] Could not create the complete Google Drive ZIP.
    echo The prepared folder is still available at dist\GoogleDrive_Package.
    exit /b 1
)

echo.
echo ============================================================
echo   PACKAGE CREATED SUCCESSFULLY IN:
echo   dist\GoogleDrive_Package\
echo ============================================================
echo.
echo HOW TO SHARE ON GOOGLE DRIVE:
echo.
echo   Option A (Recommended):
echo   Drag and drop the entire "dist\GoogleDrive_Package" folder
echo   directly into your Google Drive browser window!
echo.
echo   Option B (Just the 1-Click Installer):
echo   Upload "ClassTrack_Setup_v1.0.exe" + "START_HERE.html"
echo.
echo CONTENTS OF THE PACKAGE:
echo   1. ClassTrack_Setup_v1.0.exe: Standalone Windows App (No Python needed!)
echo   2. Open_ClassTrack_Dashboard.url: 1-Click cloud dashboard shortcut
echo   3. START_HERE.html: Visual quick-start guide
echo   4. README_NON_TECH.txt: Plain text instructions
echo   5. ClassTrack_Source_Code.zip: Clean source (~30MB) for professors/developers
echo ============================================================
echo.
pause
