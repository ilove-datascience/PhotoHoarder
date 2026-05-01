@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Virtual environment Python not found at:
  echo         "%PYTHON_EXE%"
  echo.
  echo Create it first, for example:
  echo   uv sync
  exit /b 1
)

"%PYTHON_EXE%" "%~dp0main.py" %*
exit /b %ERRORLEVEL%
