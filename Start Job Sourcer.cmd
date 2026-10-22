@echo off
REM Starts Job Sourcer in the background (if it is not running) and opens its window.
cd /d "%~dp0"
if not exist "workspace\steward.yaml" (
  echo No workspace yet. See SETUP.md, step 1.
  pause
  exit /b 1
)
".venv\Scripts\steward.exe" -c "workspace\steward.yaml" app
