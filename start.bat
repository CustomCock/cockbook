@echo off
rem CockBook per Doppelklick starten (ruft start.ps1 auf).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 pause
