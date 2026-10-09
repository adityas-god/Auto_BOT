@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo  GreyOrange OpsBot - Google SSO Session Refresher
echo ========================================================

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" save_google_session.py %*

echo.
pause
