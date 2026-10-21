@echo off
rem liteview launcher (Win10+)
rem Use: double-click an image (after register-open-with), drag files here, or:
rem   run.bat "D:\photos\a.jpg"
setlocal
set "APP_DIR=F:\p_d"
if exist "%~dp0viewer.py" set "APP_DIR=%~dp0"
if exist "%~dp0pyproject.toml" set "APP_DIR=%~dp0"
cd /d "%APP_DIR%" || exit /b 1

rem Prefer the project venv Python (faster, no uv wrapper). Fallback to uv.
set "PY=%APP_DIR%.venv\Scripts\python.exe"
if exist "%PY%" goto :run

where uv >nul 2>nul
if %ERRORLEVEL%==0 (
    if "%~1"=="" (uv run viewer.py) else (uv run viewer.py %*)
    exit /b %ERRORLEVEL%
)
echo liteview: Python venv not found and uv is not installed.
exit /b 1

:run
if "%~1"=="" (
    "%PY%" -X utf8 viewer.py
) else (
    "%PY%" -X utf8 viewer.py %*
)
