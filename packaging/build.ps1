# Build the palctl Windows binaries and installer.
#
# Prereqs: Windows, Python 3.11+, and (for the installer) Inno Setup 6.
# Usage:   powershell -ExecutionPolicy Bypass -File packaging\build.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> Creating build venv"
python -m venv .build-venv
$py = ".\.build-venv\Scripts\python.exe"

Write-Host "==> Installing palctl + PyInstaller"
& $py -m pip install --upgrade pip
& $py -m pip install -e .
& $py -m pip install pyinstaller

Write-Host "==> Building binaries with PyInstaller"
# Run from packaging\ so the spec's relative entry-script paths resolve.
Push-Location packaging
& $py -m PyInstaller --noconfirm --clean palctl.spec
Pop-Location

Write-Host "==> Binaries are in dist\palctl\"

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    Write-Host "==> Compiling installer with Inno Setup"
    & $iscc packaging\installer.iss
    Write-Host "==> Installer written to packaging\Output\palctl-setup.exe"
} else {
    Write-Host "Inno Setup 6 not found at '$iscc'."
    Write-Host "Install it from https://jrsoftware.org/isdl.php, or ship dist\palctl\ as-is."
}
