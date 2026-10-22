@echo off
REM Opens the sandbox demo (fake inbox, LinkedIn and calendar). Safe to play with.
cd /d "%~dp0"
if not exist "sandbox-demo\steward.yaml" ".venv\Scripts\steward.exe" init --sandbox --dir sandbox-demo
".venv\Scripts\steward.exe" -c "sandbox-demo\steward.yaml" app
