@echo off
setlocal
cd /d "%~dp0"
set "QQBOT_RUNTIME=runtime\QQBot"
set "QQBOT_RUNTIME_NAME="
if exist "runtime\active-runtime.txt" set /p "QQBOT_RUNTIME_NAME="<"runtime\active-runtime.txt"
if defined QQBOT_RUNTIME_NAME if exist "runtime\%QQBOT_RUNTIME_NAME%\QQBot.exe" set "QQBOT_RUNTIME=runtime\%QQBOT_RUNTIME_NAME%"
if not exist "%QQBOT_RUNTIME%\QQBot.exe" (echo QQBot runtime not found. & pause & exit /b 1)
"%QQBOT_RUNTIME%\QQBot.exe" --home "%~dp0." --module osu-spectator start 
set "QQBOT_EXIT=%ERRORLEVEL%"
echo.
echo Spectator ended with code %QQBOT_EXIT%. Check output above.
pause >nul
exit /b %QQBOT_EXIT%
