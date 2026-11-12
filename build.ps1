$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "未找到 .venv，请先运行: python -m venv .venv"
}
& $python -m pip install -r requirements-dev.txt
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name FTPMate `
    --collect-all sv_ttk `
    --collect-all tkinterdnd2 `
    --collect-all paramiko `
    main.py
Write-Host "构建完成: $root\dist\FTPMate.exe"
