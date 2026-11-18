@echo off
setlocal
cd /d "%~dp0"
set "RUNTIME="
if exist "runtime\active-runtime.txt" set /p "RUNTIME="<"runtime\active-runtime.txt"
"runtime\%RUNTIME%\QQBot.exe" --home "%~dp0." --module osu-live stop
pause
