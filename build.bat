@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv. Run: python -m venv .venv
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name FTPMate --collect-all sv_ttk --collect-all tkinterdnd2 --collect-all paramiko main.py
if errorlevel 1 exit /b 1
echo Build complete: dist\FTPMate.exe
