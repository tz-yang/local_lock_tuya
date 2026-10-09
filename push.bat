@echo off
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0push.ps1"
pause
