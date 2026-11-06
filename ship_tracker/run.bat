@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Ship Tracker
cd /d "%~dp0"

if not exist "ship_tracker.py" (
    echo В папке нет файла ship_tracker.py.
    pause
    exit /b 1
)

if not exist "%~dp0data" mkdir "%~dp0data" >nul 2>nul

set "PY="
call "%~dp0env.bat"
if errorlevel 1 goto failed

"%PY%" -X utf8 "ship_tracker.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" goto runfail
exit /b 0

:runfail
echo.
echo Приложение завершилось с ошибкой (код %CODE%).
echo Подробности — в файле data\errors.log
echo.
pause
exit /b %CODE%

:failed
echo.
echo Не удалось подготовить Python — смотрите сообщение выше.
pause
exit /b 1
