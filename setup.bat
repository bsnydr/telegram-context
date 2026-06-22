@echo off
REM First-time setup: create a virtual environment and install dependencies.
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

echo Creating virtual environment in .venv ...
%PY% -m venv .venv || (echo ERROR: could not create venv. Install Python 3.10+ from python.org ^(tick "Add python.exe to PATH"^) and re-run. & exit /b 1)

echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo ERROR: pip install failed. & exit /b 1)

echo.
echo Setup complete.
echo Next steps:
echo   1^) Copy .env.example to .env and paste your bot token.
echo   2^) Double-click run.bat ^(or run it in a terminal^) to start the bot.
endlocal
