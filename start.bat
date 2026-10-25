@echo off
rem Starts Clipboard Auto Typer without VS Code and without a console window.
rem Double-click this file, or put a shortcut to it on the desktop.
cd /d "%~dp0"

set "APP_PYTHON=python"
set "APP_PYTHONW=pythonw"
if exist "%~dp0.venv\Scripts\python.exe" (
    set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"
    set "APP_PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
)

"%APP_PYTHON%" -c "import keyboard, pyperclip, pystray, pythoncom, requests, win32com.client, win32crypt, PIL" >nul 2>nul
if errorlevel 1 (
    echo Clipboard Auto Typer cannot start: Python or a required package is missing.
    echo Install dependencies using the Python interpreter above or your project virtual environment.
    echo Run: "%APP_PYTHON%" -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if exist "%APP_PYTHONW%" (
    start "" "%APP_PYTHONW%" "%~dp0main.py"
    exit /b 0
)
where "%APP_PYTHONW%" >nul 2>nul
if not errorlevel 1 (
    start "" "%APP_PYTHONW%" "%~dp0main.py"
    exit /b 0
)
start "" "%APP_PYTHON%" "%~dp0main.py"
exit /b 0
