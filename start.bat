@echo off
rem Starts Clipboard Auto Typer without VS Code and without a console window.
rem Double-click this file, or put a shortcut to it on the desktop.
cd /d "%~dp0"

python -c "import keyboard, pyperclip, pystray, pythoncom, requests, win32com.client, PIL" >nul 2>nul
if errorlevel 1 (
    echo Clipboard Auto Typer cannot start: Python or a required package is missing.
    echo In this folder, run:  pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py"
    exit /b 0
)
start "" python "%~dp0main.py"
exit /b 0
