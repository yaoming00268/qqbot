@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title QQBot Studio

if "%QQBOT_USE_SOURCE%"=="1" goto :SOURCE
set "QQBOT_RUNTIME=%~dp0runtime\QQBot"
set "QQBOT_RUNTIME_NAME="
if exist "%~dp0runtime\active-runtime.txt" set /p "QQBOT_RUNTIME_NAME="<"%~dp0runtime\active-runtime.txt"
if defined QQBOT_RUNTIME_NAME if exist "%~dp0runtime\%QQBOT_RUNTIME_NAME%\QQBotStudio.exe" set "QQBOT_RUNTIME=%~dp0runtime\%QQBOT_RUNTIME_NAME%"
if not exist "%QQBOT_RUNTIME%\QQBotStudio.exe" goto :SOURCE
"%QQBOT_RUNTIME%\QQBotStudio.exe" --home "%~dp0." %*
goto :DONE

:SOURCE
if exist "G:\shiyan\anaconda\python.exe" (
    set "QQBOT_PYTHON=G:\shiyan\anaconda\python.exe"
    goto :RUN_SOURCE
)
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] No compiled runtime or Python environment found.
    pause
    exit /b 1
)
set "QQBOT_PYTHON=python"

:RUN_SOURCE
"%QQBOT_PYTHON%" -X utf8 -u gui.py %*

:DONE
set "QQBOT_EXIT=%ERRORLEVEL%"
if not "%QQBOT_EXIT%"=="0" (
    echo [ERROR] GUI exited with code %QQBOT_EXIT%.
    pause
)
exit /b %QQBOT_EXIT%
