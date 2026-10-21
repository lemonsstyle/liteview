# Build a portable (no-install) liteview folder for Windows 10 x64.
# Output: dist\liteview\  — double-click 启动.bat
#
# Requires: uv, network (one-time download of CPython embeddable)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root 'dist\liteview'
$pyVer = '3.12.10'
$zip = Join-Path $env:TEMP 'python-embed-liteview.zip'
$zipUrl = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-amd64.zip"

Write-Host "==> Cleaning $dist"
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dist | Out-Null

Write-Host "==> Downloading CPython $pyVer embeddable"
if (-not (Test-Path $zip)) {
    Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing -TimeoutSec 180
}
Expand-Archive -Path $zip -DestinationPath (Join-Path $dist 'python') -Force

@'
python312.zip
.
Lib\site-packages
import site
'@ | Set-Content -Encoding ascii (Join-Path $dist 'python\python312._pth')

Write-Host "==> Installing Python packages"
uv pip install --target (Join-Path $dist 'python\Lib\site-packages') pillow pygame-ce pyturbojpeg

Write-Host "==> Copying app files"
Copy-Item (Join-Path $root 'viewer.py') (Join-Path $dist 'viewer.py') -Force
$turbo = Join-Path $root 'libjpeg-turbo\bin\turbojpeg.dll'
if (Test-Path $turbo) {
    Copy-Item $turbo (Join-Path $dist 'turbojpeg.dll') -Force
} else {
    Write-Warning 'turbojpeg.dll not found — app will use Pillow (slower decode)'
}

@'
@echo off
rem liteview portable (Win10 x64) — double-click or drag images onto this file
cd /d "%~dp0"
if "%~1"=="" (
    python\python.exe -X utf8 viewer.py
) else (
    python\python.exe -X utf8 viewer.py %*
)
'@ | Set-Content -Encoding ascii (Join-Path $dist '启动.bat')

Write-Host "==> Done: $dist"
