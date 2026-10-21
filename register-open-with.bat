@echo off
rem Register liteview as an image "Open with" app for the current user.
reg import "%~dp0register-open-with.reg"
if errorlevel 1 (
    echo Registration failed.
    pause
    exit /b 1
)
echo.
echo Registered. To make it the DEFAULT when you double-click:
echo   1. Right-click a photo  -^>  Open with  -^>  Choose another app
echo   2. Pick "liteview", enable "Always use this app for .jpg files"
echo   3. Repeat for other types if needed (.png, .webp, ...)
echo.
pause
