@echo off
setlocal EnableExtensions
REM ==========================================================
REM  PDF Printer Optimizer - Windows launcher
REM  Double-click this file. It will:
REM    1. create / activate a private Python environment (.venv)
REM    2. install missing dependencies
REM    3. process every PDF in the "input" folder
REM    4. keep this window open so you can read the summary
REM ==========================================================
cd /d "%~dp0"
title PDF Printer Optimizer

REM ---- find Python 3 ----------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo  Python 3 was not found on this computer.
    echo  Install it from https://www.python.org/downloads/
    echo  and tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM ---- create the virtual environment if needed -------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment ^(first run only^)...
    %PY% -m venv --clear .venv
    if errorlevel 1 (
        echo Could not create the Python environment.
        pause
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat"

REM ---- install dependencies when missing ---------------------
python -c "import fitz, cv2, numpy, PIL, pypdf" >nul 2>&1
if errorlevel 1 (
    echo Installing required packages ^(first run only, needs internet^)...
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Package installation failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

REM ---- run ------------------------------------------------------
python main.py %*
echo.
pause
endlocal
