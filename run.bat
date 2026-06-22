@echo off
REM Start the bot in the foreground (manual / testing). Press Ctrl-C to stop.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv not found. Run setup.bat first.
  exit /b 1
)

".venv\Scripts\python.exe" telegram_context.py
endlocal
