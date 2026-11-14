@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if "%QQBOT_USE_SOURCE%"=="1" goto :SOURCE
set "QQBOT_RUNTIME=%~dp0runtime\QQBot"
set "QQBOT_RUNTIME_NAME="
if exist "%~dp0runtime\active-runtime.txt" set /p "QQBOT_RUNTIME_NAME="<"%~dp0runtime\active-runtime.txt"
if defined QQBOT_RUNTIME_NAME if exist "%~dp0runtime\%QQBOT_RUNTIME_NAME%\QQBotCLI.exe" set "QQBOT_RUNTIME=%~dp0runtime\%QQBOT_RUNTIME_NAME%"
if exist "%QQBOT_RUNTIME%\QQBotCLI.exe" goto :COMPILED

:SOURCE
if exist "G:\shiyan\anaconda\python.exe" goto :LOCAL_PYTHON
python -X utf8 console_tools\cli.py %*
exit /b %ERRORLEVEL%

:LOCAL_PYTHON
"G:\shiyan\anaconda\python.exe" -X utf8 console_tools\cli.py %*
exit /b %ERRORLEVEL%

:COMPILED
"%QQBOT_RUNTIME%\QQBotCLI.exe" --home "%~dp0." %*
exit /b %ERRORLEVEL%
