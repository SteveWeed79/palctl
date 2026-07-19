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
# Run from packaging\ so the spec's relative entry-script paths resolve, but send
# the output to the repo-root dist\ / build\ so installer.iss (which references
# ..\dist from packaging\) finds it.
Push-Location packaging
& $py -m PyInstaller --noconfirm --clean --distpath ..\dist --workpath ..\build palctl.spec
Pop-Location

# Ship WinSW inside the build, verified here against the pin in
# palctl/winservice.py (single source of truth). Installer users then never
# download the service wrapper at install time — which is exactly where fresh
# Windows boxes (sparse root-cert store) and AV HTTPS-scanning used to kill
# setup with CERTIFICATE_VERIFY_FAILED.
Write-Host "==> Bundling WinSW (hash-verified) into dist\palctl\"
$winswUrl = & $py -c "from palctl.winservice import WINSW_URL; print(WINSW_URL)"
$winswSha = & $py -c "from palctl.winservice import WINSW_SHA256; print(WINSW_SHA256)"
$winswDest = "dist\palctl\winsw.exe"
Invoke-WebRequest -Uri $winswUrl -OutFile $winswDest -UseBasicParsing
$actual = (Get-FileHash -Algorithm SHA256 $winswDest).Hash.ToLower()
if ($actual -ne $winswSha.ToLower()) {
    Remove-Item $winswDest
    throw "WinSW hash mismatch: expected $winswSha, got $actual — refusing to bundle."
}

Write-Host "==> Binaries are in dist\palctl\"

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    Write-Host "==> Compiling installer with Inno Setup"
    # Same version injection as the release workflow: palctl/__init__.py is
    # the single source, so local builds report the right version too.
    $ver = & $py -c "import palctl; print(palctl.__version__)"
    & $iscc /DAppVersion=$ver packaging\installer.iss
    Write-Host "==> Installer written to packaging\Output\palctl-setup.exe"
} else {
    Write-Host "Inno Setup 6 not found at '$iscc'."
    Write-Host "Install it from https://jrsoftware.org/isdl.php, or ship dist\palctl\ as-is."
}
