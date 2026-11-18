@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if not defined QQBOT_HOME set "QQBOT_HOME=%~dp0."
set "QQBOT_RUNTIME=%~dp0runtime\QQBot"
set "QQBOT_RUNTIME_NAME="
if exist "%~dp0runtime\active-runtime.txt" set /p "QQBOT_RUNTIME_NAME="<"%~dp0runtime\active-runtime.txt"
if defined QQBOT_RUNTIME_NAME if exist "%~dp0runtime\%QQBOT_RUNTIME_NAME%\QQBot.exe" set "QQBOT_RUNTIME=%~dp0runtime\%QQBOT_RUNTIME_NAME%"
if "%QQBOT_USE_SOURCE%"=="1" goto :SOURCE
if not exist "%QQBOT_RUNTIME%\QQBot.exe" goto :SOURCE
"%QQBOT_RUNTIME%\QQBot.exe" --home "%QQBOT_HOME%" --module voice %*
exit /b %errorlevel%
:SOURCE
set "QQBOT_VOICE_PYTHON=python"
if exist "G:\shiyan\anaconda\python.exe" set "QQBOT_VOICE_PYTHON=G:\shiyan\anaconda\python.exe"
"%QQBOT_VOICE_PYTHON%" -X utf8 voice_cli.py --home "%QQBOT_HOME%" %*
exit /b %errorlevel%
