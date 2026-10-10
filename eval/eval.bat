@echo off
rem Alidade evaluation - runs eval.ps1 without changing the PowerShell execution policy.
rem Usage: eval\eval.bat smoke | new | old | compare | all | report | help  [-Model ... -ApiBase ...]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0eval.ps1" %*
set EXITCODE=%ERRORLEVEL%
rem Keep the window open when started by double-click (no arguments).
if "%~1"=="" pause
exit /b %EXITCODE%
