@echo off
setlocal
cd /d "%~dp0"
set "RUNTIME="
if exist "runtime\active-runtime.txt" set /p "RUNTIME="<"runtime\active-runtime.txt"
if not exist "runtime\%RUNTIME%\QQBot.exe" (echo Runtime missing. & pause & exit /b 1)
start "" /b "runtime\%RUNTIME%\QQBot.exe" --home "%~dp0." --module osu-live start
