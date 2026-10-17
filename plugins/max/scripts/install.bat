@echo off
rem Wrapper so the installer can be double-clicked. All arguments pass through to install.ps1.
powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
