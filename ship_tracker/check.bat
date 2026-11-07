@echo off
rem Файл должен оставаться с переводами строк CRLF (см. .gitattributes).
chcp 65001 >nul
setlocal EnableExtensions
title Ship Tracker — проверка запуска
cd /d "%~dp0"

if not exist "%~dp0data" mkdir "%~dp0data" >nul 2>nul

set "PY="
call "%~dp0env.bat" app
if errorlevel 1 goto manual

"%PY%" -X utf8 "%~dp0diagnose.py"
goto show

:manual
echo.
echo Окружение не готово — записываю общую информацию о компьютере.
> "%~dp0data\diagnose.txt" (
    echo Ship Tracker — отчёт о запуске
    echo Дата: %DATE% %TIME%
    echo Компьютер: %COMPUTERNAME%
    ver
    echo.
    echo --- где ищем Python ---
    where py 2^>^&1
    where python 2^>^&1
    echo.
    echo --- путь программы ---
    echo %~dp0
    echo.
    echo --- журнал подготовки окружения ---
    type "%LOCALAPPDATA%\ShipTracker\env.log" 2^>^&1
)

:show
echo.
echo Отчёт сохранён: %~dp0data\diagnose.txt
echo Его можно переслать, если приложение не открывается.
start "" notepad "%~dp0data\diagnose.txt"
pause
