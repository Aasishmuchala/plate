@echo off
rem The plate command line from a checkout (the Windows twin of scripts/plate): run, gate, export, reindex,
rem gc, pack, smoke. Exit codes: 0 ok, 1 error, 2 gate fail, 3 custody or budget refusal, 4 usage.
setlocal
set "PLATE_REPO=%~dp0.."
set "PLATE_PY=%PLATE_REPO%\.venv\Scripts\python.exe"
if not exist "%PLATE_PY%" (
    echo No .venv: run scripts\bootstrap.ps1 first 1>&2
    exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONPATH=%PLATE_REPO%\spec;%PLATE_REPO%\daemon;%PLATE_REPO%\pixels;%PLATE_REPO%\engines;%PLATE_REPO%\gates;%PLATE_REPO%\plugins\max;%PLATE_REPO%\tools"
"%PLATE_PY%" -m plate_daemon.cli %*
exit /b %ERRORLEVEL%
