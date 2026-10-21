@echo off
rem liteview launcher (Win10+)
rem Double-click, or drag image files onto this bat.
rem
rem App lives in APP_DIR. This bat can be copied into any photo folder.
setlocal
set "APP_DIR=F:\p_d"

rem If this bat sits inside the app folder (full package copy), use it.
if exist "%~dp0viewer.py" set "APP_DIR=%~dp0"
if exist "%~dp0pyproject.toml" set "APP_DIR=%~dp0"

cd /d "%APP_DIR%" || exit /b 1

if "%~1"=="" (
    uv run viewer.py
) else (
    uv run viewer.py %*
)
