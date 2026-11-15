@echo off
setlocal
cd /d "%~dp0"
title agy2api

rem If uv was installed after this shell started, find it in the winget package folder
where uv >nul 2>nul
if errorlevel 1 (
    for /d %%d in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*") do set "PATH=%PATH%;%%~d"
)

where uv >nul 2>nul
if errorlevel 1 (
    echo [error] uv was not found. Install it with: winget install astral-sh.uv
    pause
    exit /b 1
)

if not exist ".env" echo [warn] No .env file found - server will use defaults: all interfaces, password pwd

echo Syncing dependencies...
uv sync --quiet
if errorlevel 1 (
    echo [error] Dependency sync failed.
    pause
    exit /b 1
)

echo Starting agy2api...
uv run --no-sync python web.py
if errorlevel 1 (
    echo.
    echo [error] Server exited with an error.
    pause
    exit /b 1
)
endlocal